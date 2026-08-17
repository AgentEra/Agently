import asyncio
import threading

import pytest
from agently_stage import Stage
from agently.utils import FunctionShifter, filter_callable_options


def test_kwargs():
    def test_func(*, a: str, b: int):
        return int(a) + b

    options = {
        "a": "1",
        "b": 2,
        "c": 3,
    }

    with pytest.raises(Exception):
        test_func(**options)

    new_test_func = FunctionShifter.auto_options_func(test_func)
    assert new_test_func(**options) == 3


def test_filter_callable_options_is_the_non_runtime_owner():
    def test_func(*, a: str, b: int):
        return int(a) + b

    filtered = filter_callable_options(test_func)
    assert filtered(a="1", b=2, ignored=True) == 3


def test_syncify_delegates_to_scoped_stage_adapter(monkeypatch):
    observed = []

    def fake_as_sync(func):
        observed.append(func)
        return lambda value: func(value)

    async def source(value):
        return value

    monkeypatch.setattr(Stage, "as_sync", fake_as_sync)
    with pytest.warns(DeprecationWarning, match="Stage.as_sync/as_async"):
        adapted = FunctionShifter.syncify(source)

    assert observed == [source]
    coroutine = adapted("ok")
    assert asyncio.run(coroutine) == "ok"


def test_asyncify_delegates_to_scoped_stage_adapter(monkeypatch):
    observed = []

    def fake_as_async(func):
        observed.append(func)

        async def adapted(value):
            return func(value)

        return adapted

    def source(value):
        return value

    monkeypatch.setattr(Stage, "as_async", fake_as_async)
    with pytest.warns(DeprecationWarning, match="Stage.as_sync/as_async"):
        adapted = FunctionShifter.asyncify(source)

    assert observed == [source]
    assert asyncio.run(adapted("ok")) == "ok"


@pytest.mark.asyncio
async def test_asyncify_sync_generator_yields_items():
    def sync_gen():
        for i in range(3):
            yield i

    result = []
    async for item in FunctionShifter.asyncify_sync_generator(sync_gen()):
        result.append(item)

    assert result == [0, 1, 2]


@pytest.mark.asyncio
async def test_asyncify_sync_generator_raises_error():
    def sync_gen():
        yield "ok"
        raise ValueError("boom")

    result = []
    with pytest.raises(ValueError, match="boom"):
        async for item in FunctionShifter.asyncify_sync_generator(sync_gen()):
            result.append(item)

    assert result == ["ok"]


@pytest.mark.asyncio
async def test_asyncify_sync_generator_closes_on_break():
    closed = threading.Event()

    def sync_gen():
        try:
            i = 0
            while True:
                yield i
                i += 1
        finally:
            closed.set()

    async for item in FunctionShifter.asyncify_sync_generator(sync_gen()):
        assert item == 0
        break

    assert await asyncio.to_thread(closed.wait, 5.0)
