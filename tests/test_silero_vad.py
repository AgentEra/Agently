"""Adapter ABI/lifecycle tests with a fake ONNX session, not VAD quality tests."""

# ruff: noqa: E402 -- optional dependencies are checked before adapter import

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

np = pytest.importorskip('numpy')
pytest.importorskip('onnxruntime')

from agently import AudioProtocolError, PCMFormat
from agently.integrations.silero import SileroVAD


class Model:
    def __init__(self):
        self.inputs = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.wait = False

    def get_inputs(self):
        return [SimpleNamespace(name=name) for name in ('input','state','sr')]

    def run(self, names, inputs):
        self.entered.set()
        if self.wait:
            self.release.wait(timeout=5)
        self.inputs.append(inputs)
        return [np.array([[.75]],dtype=np.float32), inputs['state']+1]


def make(monkeypatch,tmp_path):
    import agently.integrations.silero as module
    model=Model()
    monkeypatch.setattr(module.ort,'InferenceSession',lambda *a,**k:model)
    path=tmp_path/'fixture.onnx'
    path.write_bytes(b'explicit fake ONNX loader')
    return SileroVAD(path),model


@pytest.mark.asyncio
async def test_fresh_states_context_and_analysis_padding(monkeypatch,tmp_path):
    detector,model=make(monkeypatch,tmp_path)
    async with detector.open(PCMFormat()) as one, detector.open(PCMFormat()) as two:
        assert one.frame_samples == two.frame_samples == 512
        assert await one.score(b'\x01\x00'*512) == .75
        assert await one.score(b'\x01\x00'*7) == .75
        assert await two.score(b'\x01\x00'*512) == .75
    assert model.inputs[0]['state'].sum() == model.inputs[2]['state'].sum() == 0
    assert model.inputs[1]['state'].sum() == 256
    assert model.inputs[0]['input'].shape == (1,576)
    assert np.all(model.inputs[1]['input'][0,64+7:] == 0)
    async with detector.open(PCMFormat(8000)) as session:
        assert session.frame_samples == 256
        await session.score(b'\x00\x00'*256)
    assert model.inputs[-1]['input'].shape == (1,288)


@pytest.mark.asyncio
@pytest.mark.parametrize('fmt',[PCMFormat(24000),PCMFormat(16000,2)])
async def test_explicit_format_rejection(monkeypatch,tmp_path,fmt):
    detector,model=make(monkeypatch,tmp_path)
    with pytest.raises(ValueError,match='mono s16le'):
        async with detector.open(fmt):
            pass
    assert not model.inputs


@pytest.mark.asyncio
async def test_repeated_cancel_settles_started_cpu_frame(monkeypatch,tmp_path):
    detector,model=make(monkeypatch,tmp_path)
    model.wait=True
    async with detector.open(PCMFormat()) as session:
        task=asyncio.create_task(session.score(b'\x00\x00'*512))
        while not model.entered.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(.005)
        task.cancel()
        await asyncio.sleep(.005)
        assert not task.done()
        model.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task,1)
    assert len(model.inputs)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('data',[b'',b'a',b'\x00\x00'*513])
async def test_analysis_frame_shape_rejection(monkeypatch,tmp_path,data):
    detector,model=make(monkeypatch,tmp_path)
    async with detector.open(PCMFormat()) as session:
        with pytest.raises(ValueError,match='complete mono'):
            await session.score(data)
    assert not model.inputs


def test_missing_local_model_is_not_downloaded(tmp_path):
    with pytest.raises(FileNotFoundError):
        SileroVAD(tmp_path/'missing.onnx')


@pytest.mark.asyncio
async def test_invalid_onnx_outputs_fail_explicitly(monkeypatch,tmp_path):
    detector,model=make(monkeypatch,tmp_path)
    monkeypatch.setattr(model,'run',lambda *args: [np.array([.5],dtype=np.float32)])
    async with detector.open(PCMFormat()) as session:
        with pytest.raises(AudioProtocolError,match='output'):
            await session.score(b'\x00\x00'*512)
