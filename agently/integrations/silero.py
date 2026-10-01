"""Explicit local Silero VAD v5/v6 ONNX adapter (no Torch or model downloads).

Install numpy and onnxruntime, obtain a trusted upstream silero_vad.onnx file,
then construct SileroVAD(model_path) before entering a latency-sensitive loop.
Model loading is synchronous and explicit. Each open() owns fresh recurrent
state; a shared inference session is read-only. See docs/models/audio.md.
"""

# ruff: noqa: E402 -- optional dependency guard must run before imports

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from os import PathLike
from pathlib import Path
from typing import cast

from agently.types.data.audio import AudioProtocolError, PCMFormat
from agently.types.plugins.AudioModelRequester import SpeechDetectionSession
from agently.utils import LazyImport

LazyImport.import_package("numpy", version_constraint=">=1.24,<3")
LazyImport.import_package("onnxruntime", version_constraint=">=1.16,<2")

import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray


class _SileroSession:
    def __init__(self, model: ort.InferenceSession, rate: int):
        self.model = model
        self.rate = rate
        self._frame_samples = 512 if rate == 16000 else 256
        self.context_size = 64 if rate == 16000 else 32
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, self.context_size), dtype=np.float32)

    @property
    def frame_samples(self) -> int:
        return self._frame_samples

    def _score(self, pcm: bytes) -> float:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        # Padding affects only the last analysis frame, never the STT samples
        # or source timeline. Full frames keep recurrent context continuous.
        samples = np.pad(samples, (0, self.frame_samples - len(samples))).reshape(1, -1)
        data = np.concatenate((self.context, samples), axis=1)
        values = cast(list[NDArray[np.float32]], self.model.run(None, {
            "input": data, "state": self.state, "sr": np.array(self.rate, dtype=np.int64),
        }))
        if len(values) != 2 or values[0].size != 1 or values[1].shape != (2, 1, 128):
            raise AudioProtocolError("Unsupported Silero ONNX output; use the v5/v6 input/state/sr model.")
        self.state = values[1]
        self.context = data[:, -self.context_size:].copy()
        return float(values[0].reshape(-1)[0])

    async def score(self, pcm: bytes) -> float:
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2 or len(pcm) > self.frame_samples * 2:
            raise ValueError("Silero expects 1..frame_samples complete mono s16le frames.")
        task = asyncio.create_task(asyncio.to_thread(self._score, pcm))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # CPU work cannot be force-cancelled safely. Settle it before
            # releasing this session; do not abandon a running worker.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            # Retrieve a worker failure without replacing caller cancellation.
            if not task.cancelled():
                task.exception()
            raise


class SileroVAD:
    """Shareable detector using an explicitly supplied local ONNX model.

    Supports mono s16le at 8/16 kHz only. Never records, downloads, resamples,
    changes global Torch state or loads an ASR model. Callbacks/ASR are owned
    by AudioModelRequest, not this acoustic adapter.
    """

    def __init__(self, model_path: str | PathLike[str]):
        path = Path(model_path)
        if not path.is_file():
            raise FileNotFoundError(f"Silero ONNX model not found: {path}")
        settings = ort.SessionOptions()
        settings.inter_op_num_threads = 1
        settings.intra_op_num_threads = 1
        self.__model = ort.InferenceSession(str(path), sess_options=settings, providers=["CPUExecutionProvider"])
        if {value.name for value in self.__model.get_inputs()} != {"input", "state", "sr"}:
            raise AudioProtocolError("SileroVAD requires the v5/v6 ONNX input/state/sr model.")

    @asynccontextmanager
    async def open(self, audio_format: PCMFormat) -> AsyncIterator[SpeechDetectionSession]:
        if audio_format.encoding != "s16le" or audio_format.channels != 1 or audio_format.sample_rate not in (8000, 16000):
            raise ValueError("SileroVAD requires mono s16le PCM at 8000 or 16000 Hz; convert explicitly.")
        yield _SileroSession(self.__model, audio_format.sample_rate)
