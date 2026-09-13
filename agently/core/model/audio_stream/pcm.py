"""Strict PCM WAV conversion for stream composition; no implicit resampling."""

from __future__ import annotations

import io
import struct
import wave

from agently.types.data.audio import AudioInput, AudioProtocolError, PCMFormat, SpeechResult
from .segmentation import positive_int


def validate_pcm(value: PCMFormat) -> int:
    positive_int(value.sample_rate, "sample_rate")
    positive_int(value.channels, "channels")
    if value.encoding != "s16le":
        raise ValueError("Only PCM s16le is supported; convert explicitly before streaming.")
    return value.channels * 2


def encode_wav(data: bytes, fmt: PCMFormat) -> AudioInput:
    frame_bytes = validate_pcm(fmt)
    if not data or len(data) % frame_bytes:
        raise AudioProtocolError("PCM must contain complete non-empty sample frames.")
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setparams((fmt.channels, 2, fmt.sample_rate, 0, "NONE", "not compressed"))
        writer.writeframes(data)
    return AudioInput(out.getvalue())


def decode_pcm(result: SpeechResult, expected: PCMFormat | None, *, raw: bool, allow_empty: bool = False) -> tuple[bytes, PCMFormat]:
    data = result.data
    if raw:
        if expected is None:
            raise ValueError("Raw PCM output requires an explicit audio_format.")
        frame_bytes = validate_pcm(expected)
        if not data or len(data) % frame_bytes:
            raise AudioProtocolError("Invalid raw PCM frame length.")
        return data, expected
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioProtocolError("Continuous TTS requires PCM WAV (or explicitly configured raw PCM).")
    size = struct.unpack_from("<I", data, 4)[0]
    if size + 8 != len(data):
        raise AudioProtocolError("WAV container length does not match received bytes.")
    offset = 12
    fmt: PCMFormat | None = None
    samples: bytes | None = None
    while offset < len(data):
        if offset + 8 > len(data):
            raise AudioProtocolError("Incomplete WAV chunk header.")
        name = data[offset:offset + 4]
        length = struct.unpack_from("<I", data, offset + 4)[0]
        start, end = offset + 8, offset + 8 + length
        if end + length % 2 > len(data):
            raise AudioProtocolError("Incomplete WAV chunk payload.")
        if name == b"fmt ":
            if fmt is not None or length < 16:
                raise AudioProtocolError("Invalid/duplicate WAV format chunk.")
            encoding, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", data, start)
            if encoding != 1 or bits != 16:
                raise AudioProtocolError("Only uncompressed 16-bit PCM WAV is supported.")
            fmt = PCMFormat(rate, channels)
            frame_bytes = validate_pcm(fmt)
            if align != frame_bytes or byte_rate != rate * frame_bytes:
                raise AudioProtocolError("WAV format has inconsistent frame/byte rate.")
        elif name == b"data":
            if samples is not None:
                raise AudioProtocolError("Multiple WAV data chunks are unsupported.")
            samples = data[start:end]
        offset = end + length % 2
    if fmt is None or samples is None or (not samples and not allow_empty) or len(samples) % (fmt.channels * 2):
        raise AudioProtocolError("WAV has no valid complete PCM frames.")
    if expected is not None and expected != fmt:
        raise AudioProtocolError("TTS sample format changed; explicit conversion is required.")
    return samples, fmt
