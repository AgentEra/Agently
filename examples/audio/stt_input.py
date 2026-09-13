"""Explicit VAD + unchanged raw STT, without microphone or business cleanup.

Requires Agently 4.1.4.9 development, numpy, onnxruntime and a trusted local
Silero v5/v6 ONNX file. No model download or dependency installation is implicit.
Set AUDIO_BASE_URL, AUDIO_STT_MODEL, SILERO_VAD_MODEL_PATH, optionally AUDIO_API_KEY.
Run: python examples/audio/stt_input.py path/to/mono-16k-s16le.wav

PCM -> acoustic probability/segments -> real STT -> raw blocks -> ordered pause.
Expected key output (2026-09-13, local Qwen3-ASR-0.6B-4bit, upstream Silero
60-second test.wav; upstream commit 867c2aa692646a1f1de3e94a15c9dd9f614c0acb):
STT requests: 9 submitted seconds: 53.396
First raw block: 0.000..2.166; last: 50.954..60.000; input_end: 60.0.
This is a recorded-file run, not microphone-noise or real-time-latency proof.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import wave
from collections.abc import AsyncIterator

from agently import Agently, AudioInputEvent, AudioInputOptions, PCMFormat, TranscriptionOptions
from agently.integrations.silero import SileroVAD


async def main(path: str, detector: SileroVAD) -> None:
    audio = Agently.create_audio_request(
        driver="OpenAICompatible", base_url=os.environ["AUDIO_BASE_URL"],
        api_key=os.getenv("AUDIO_API_KEY", ""), stt_model=os.environ["AUDIO_STT_MODEL"],
    )
    requests = 0
    submitted_seconds = 0.0

    async def event(item: AudioInputEvent) -> None:
        if item.kind in ("pause", "rejected", "input_end"):
            print(item.kind, item.start_seconds, item.end_seconds, "last_block", item.last_block)

    with wave.open(path, "rb") as reader:
        if reader.getsampwidth() != 2 or reader.getcomptype() != "NONE":
            raise ValueError("Decode the file to PCM s16le WAV explicitly.")
        fmt = PCMFormat(reader.getframerate(), reader.getnchannels())

        async def chunks() -> AsyncIterator[bytes]:
            # Bounded local-file reads; a live capture adapter supplies its own
            # async transport and overflow handling instead.
            while data := reader.readframes(2048):
                yield data

        async with audio.stream_stt(chunks(), audio_format=fmt, options=TranscriptionOptions(
            input_options=AudioInputOptions(detector=detector, on_event=event),
        )) as stream:
            async for block in stream:
                requests += 1
                submitted_seconds += block.end_seconds - block.start_seconds
                print(block.index, block.speech_index, block.reason,
                      f"{block.start_seconds:.3f}..{block.end_seconds:.3f}", repr(block.text))
    print("STT requests:", requests, "submitted seconds:", round(submitted_seconds, 3))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav")
    args = parser.parse_args()
    detector = SileroVAD(os.environ["SILERO_VAD_MODEL_PATH"])
    asyncio.run(main(args.wav, detector))
