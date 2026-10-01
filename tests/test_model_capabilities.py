"""Routing and lifecycle protocol fixtures, not model-quality evidence."""

from copy import deepcopy

import pytest

from agently.utils import Settings
from agently.utils.ModelPool import resolve_role_profile
from test_builtin_agent_executions import ScriptedExecutionRequester, create_execution_agent


class VisionRequester(ScriptedExecutionRequester):
    name = "VisionRequester"


def make_agent(tmp_path, llm=(), vlm=(), *, roles="both"):
    agent = create_execution_agent(tmp_path, "model-uses", list(llm))
    VisionRequester.reset(list(vlm))
    agent.plugin_manager.register("ModelRequester", VisionRequester, activate=False)
    if roles in {"both", "llm"}:
        agent.set_settings("llm", {"provider": "ScriptedExecutionRequester", "model": "main"})
    else:
        agent.set_settings("plugins.ModelRequester.activate", "OpenAICompatible")
    if roles in {"both", "vlm"}:
        agent.set_settings("vlm", {"provider": "VisionRequester", "model": "eyes"})
    return agent


def image(execution, **kwargs):
    return execution.image(url="https://example.test/image.png", **kwargs)


def test_profile_isolation_and_reference():
    settings = Settings()
    settings.set("plugins.ModelRequester.OpenAICompatible", {"api_key": "wrong", "headers": {"x": "wrong"}})
    settings.set("vlm", {"provider": "OpenAICompatible", "model": "eyes"})
    assert resolve_role_profile("vlm", settings) == ("OpenAICompatible", {"_api_key_pool_runtime": None, "model": "eyes"})
    settings.set("model_pool", {"small": {"provider": "OpenAICompatible", "model": "eyes", "api_key": "right"}})
    settings.set("vlm", {"model_key": "small"})
    selected = resolve_role_profile("vlm", settings)
    assert selected is not None and selected[1]["api_key"] == "right"
    settings.set("vlm", {"model_key": "small", "model": "other"})
    with pytest.raises(ValueError, match="cannot be combined"):
        resolve_role_profile("vlm", settings)


@pytest.mark.parametrize("roles", ["llm", "vlm"])
def test_single_model_structured_image(tmp_path, roles):
    agent = make_agent(tmp_path, [{"answer": "left"}], [{"answer": "left"}], roles=roles)
    execution = image(agent).input("Where?").output({"answer": str})
    assert execution.get_data() == {"answer": "left"}
    assert execution.get_text() == '{"answer": "left"}'
    assert ScriptedExecutionRequester.model_dispatches + VisionRequester.model_dispatches == 1


def test_pipeline_evidence_and_final_schema(tmp_path):
    evidence = {"observations": "Image 1 has text LEFT", "uncertainties": ["small print unreadable"]}
    agent = make_agent(tmp_path, [{"answer": "left"}], [evidence])
    execution = image(agent, question="Read the note").input("Where?").output({"answer": str})
    assert execution.get_data() == {"answer": "left"}
    assert VisionRequester.requests[0]["input"] == "Where?"
    assert "uncertainties" in str(VisionRequester.requests[0]["output"])
    assert not ScriptedExecutionRequester.requests[0]["attachment"]
    assert "small print unreadable" in str(ScriptedExecutionRequester.requests[0]["info"])
    assert execution.get_meta().get("media", {})["stages"][0]["status"] == "completed"


@pytest.mark.parametrize("method", ["to_text", "vlm_only"])
def test_explicit_direct_reuses_result(tmp_path, method):
    agent = make_agent(tmp_path, [], [{"answer": "left"}])
    execution = image(agent).input("Where?").output({"answer": str})
    if method == "vlm_only":
        execution.vlm_only(True)
        text = execution.get_text()
    else:
        text = execution.to_text()
    assert text == '{"answer": "left"}'
    assert execution.get_data() == {"answer": "left"}
    assert VisionRequester.model_dispatches == 1
    assert ScriptedExecutionRequester.model_dispatches == 0
    with pytest.raises((RuntimeError, ValueError)):
        execution.vlm_only(False)


def test_append_local_questions_and_default_description(tmp_path):
    agent = make_agent(tmp_path, [], ["description"])
    execution = image(image(agent, question="first"), question="second")
    parts = execution.request.prompt.get("attachment")
    assert [part["text"] for part in parts if part["type"] == "text"] == ["first", "second"]
    assert execution.to_text() == "description"
    agent = make_agent(tmp_path, [], ["description"])
    assert image(agent).to_text() == "description"
    assert "readable text" in str(VisionRequester.requests[0]["instruct"])


def test_mode_llm_and_conflict(tmp_path):
    agent = make_agent(tmp_path, ["answer"], [])
    assert image(agent, mode="llm").get_data() == "answer"
    assert VisionRequester.model_dispatches == 0
    with pytest.raises(ValueError, match="conflicts"):
        image(agent, mode="llm").vlm_only().get_data()


def test_invalid_profile_and_disabled_vision_before_dispatch(tmp_path):
    agent = make_agent(tmp_path, [], [], roles="llm")
    agent.set_settings("llm.vision", False)
    with pytest.raises(ValueError, match="disables vision"):
        image(agent).get_data()
    assert ScriptedExecutionRequester.model_dispatches == 0
    agent.set_settings("vlm", {"provider": "VisionRequester"})
    with pytest.raises(ValueError, match="vlm.provider"):
        image(agent).get_data()


def test_role_profile_applies_to_ordinary_model_request(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, ["answer"], [], roles="llm")
    configs = []
    old = ScriptedExecutionRequester.generate_request_data
    def capture(self):
        configs.append(deepcopy(self.settings.get("plugins.ModelRequester.ScriptedExecutionRequester")))
        return old(self)
    monkeypatch.setattr(ScriptedExecutionRequester, "generate_request_data", capture)
    agent.set_settings("plugins.ModelRequester.ScriptedExecutionRequester.api_key", "should-not-leak")
    assert agent.create_request().input("question").get_data() == "answer"
    assert configs[0]["model"] == "main"
    assert configs[0].get("api_key") is None


class AudioDriver:
    name = "AudioDriver"
    DEFAULT_SETTINGS = {}
    supported_operations = frozenset({"tts", "stt"})
    calls = []

    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    async def tts(self, request):
        from agently.types.data.audio import SpeechResult
        type(self).calls.append(("tts", request, self.connection))
        return SpeechResult(b"synthetic-audio", "audio/wav", request.model)

    async def stt(self, request):
        from agently.types.data.audio import TranscriptResult
        type(self).calls.append(("stt", request, self.connection))
        return TranscriptResult("spoken question", request.model)


def audio_agent(tmp_path, responses):
    agent = make_agent(tmp_path, responses, [], roles="llm")
    agent.plugin_manager.register("AudioModelRequester", AudioDriver, activate=False)
    AudioDriver.calls = []
    agent.set_settings("stt", {"provider": "AudioDriver", "model": "ears", "api_key": "stt-key"})
    agent.set_settings("tts", {"provider": "AudioDriver", "model": "voice", "api_key": "tts-key", "request_options": {"voice": "speaker"}})
    return agent


def test_audio_input_task_and_say_cached(tmp_path):
    from agently.types.data.audio import AudioInput
    agent = audio_agent(tmp_path, [{"answer": "final text"}])
    execution = agent.input(file=AudioInput(b"input"), type="audio").instruct("Answer briefly").output({"answer": str})
    speech = execution.say()
    assert speech is not None
    assert speech.data == b"synthetic-audio"
    assert execution.say() is speech
    assert execution.get_data() == {"answer": "final text"}
    assert [call[0] for call in AudioDriver.calls] == ["stt", "tts"]
    assert AudioDriver.calls[0][2].api_key == "stt-key"
    assert AudioDriver.calls[1][2].api_key == "tts-key"
    assert AudioDriver.calls[1][1].text == '{"answer": "final text"}'
    assert AudioDriver.calls[1][1].voice == "speaker"
    assert "spoken question" in str(ScriptedExecutionRequester.requests[0]["info"])
    assert ScriptedExecutionRequester.model_dispatches == 1


def test_input_mapping_remains_business_data(tmp_path):
    agent = make_agent(tmp_path, ["answer"], [], roles="llm")
    data = {"type": "audio", "file": "business-label"}
    assert agent.input(data).get_data() == "answer"
    assert ScriptedExecutionRequester.requests[0]["input"] == data


def test_missing_speech_dependency_does_not_start_model(tmp_path):
    agent = make_agent(tmp_path, [], [], roles="llm")
    with pytest.raises(RuntimeError, match="tts.provider"):
        agent.input("task").say()
    assert ScriptedExecutionRequester.model_dispatches == 0


def test_direct_audio_profiles_are_independent(tmp_path):
    from agently.types.data.audio import AudioInput
    agent = audio_agent(tmp_path, [])
    assert agent.stt(AudioInput(b"input")).model == "ears"
    assert agent.tts("text").model == "voice"
    assert ScriptedExecutionRequester.model_dispatches == 0


@pytest.mark.asyncio
async def test_say_all_filters_raw_reasoning_and_final_delta(tmp_path):
    agent = audio_agent(tmp_path, ["accepted final"])
    execution = agent.input("question")
    await execution.emit_stream("progress", {"message": "Public progress."}, meta={"stream_kind": "progress"})
    await execution.emit_stream("delta", "raw JSON", source="model_request")
    await execution.emit_stream("reasoning_delta", "private deliberation", source="model_request")
    await execution.async_say(scope="all")
    text = AudioDriver.calls[0][1].text
    assert "Public progress." in text and "accepted final" in text
    assert "raw JSON" not in text and "private deliberation" not in text
    assert text.count("accepted final") == 1


class OCRRequester(ScriptedExecutionRequester):
    name = "OCRRequester"


def test_ocr_direct_and_pipeline(tmp_path):
    agent = make_agent(tmp_path, ["final"], [], roles="llm")
    OCRRequester.reset(["read text", "read text again"])
    agent.plugin_manager.register("ModelRequester", OCRRequester, activate=False)
    agent.set_settings("ocr", {"provider": "OCRRequester", "model": "ocr"})
    assert image(agent, mode="ocr").to_text() == "read text"
    assert ScriptedExecutionRequester.model_dispatches == 0
    assert image(agent, mode="ocr").input("Summarize").get_data() == "final"
    assert "read text again" in str(ScriptedExecutionRequester.requests[0]["info"])
    assert OCRRequester.model_dispatches == 2


def test_ocr_rejects_unsupported_direct_reasoning(tmp_path):
    agent = make_agent(tmp_path, [], [], roles="llm")
    agent.set_settings("ocr", {"provider": "OCRRequester", "model": "ocr"})
    with pytest.raises(ValueError, match="extracts text only"):
        image(agent, mode="ocr").output({"answer": str}).to_text()


@pytest.mark.asyncio
async def test_mistral_ocr_adapter_contract():
    from agently import Agently
    from agently.builtins.plugins.ModelRequester.MistralOCR import MistralOCR
    request = Agently.create_request().image(url="https://example.test/note.png")
    request.set_settings("plugins.ModelRequester.MistralOCR", {"model":"ocr", "base_url":"https://ocr.test/v1", "api_key":"key"})
    plugin = MistralOCR(request.prompt, request.settings)
    data = plugin.generate_request_data()
    assert data.request_url == "https://ocr.test/v1/ocr"
    assert data.data["document"] == {"type":"image_url", "image_url":"https://example.test/note.png"}
    async def response():
        yield "response", {"model":"ocr", "pages":[{"markdown":"first"},{"markdown":"second"}]}
    events = [item async for item in plugin.broadcast_response(response())]
    assert ("done", "first\n\nsecond") in events


@pytest.mark.asyncio
@pytest.mark.parametrize("rows,error", [
    ([{"index":1,"embedding":[3,4]},{"index":0,"embedding":[1,2]}], None),
    ([{"index":0,"embedding":[1,2]},{"index":0,"embedding":[3,4]}], "indexes"),
    ([{"index":0,"embedding":[1,2]}], "exactly one"),
    ([{"index":0,"embedding":[1,2]},{"index":1,"embedding":[3]}], "dimensions"),
    ([{"index":0,"embedding":[float('nan')]},{"index":1,"embedding":[3]}], "finite"),
])
async def test_embedding_shape_and_order(tmp_path, monkeypatch, rows, error):
    from agently.core.model import ModelRequest
    agent = make_agent(tmp_path, [], [], roles="llm")
    agent.set_settings("embeddings", {"provider":"ScriptedExecutionRequester","model":"vector"})
    class Result:
        async def async_get_data(self, **kwargs):
            return {"data":rows}
    def result(self, **kwargs):
        assert self.settings.get("plugins.ModelRequester.ScriptedExecutionRequester.model") == "vector"
        return Result()
    monkeypatch.setattr(ModelRequest, "get_result", result)
    if error:
        with pytest.raises(ValueError, match=error):
            await agent.async_embed(["first", "second"])
    else:
        assert await agent.async_embed(["first", "second"]) == [[1.,2.],[3.,4.]]


def test_media_retry_does_not_repeat_successful_stt(tmp_path, monkeypatch):
    import httpx
    from agently.types.data.audio import AudioInput
    agent = audio_agent(tmp_path, ["final"])
    VisionRequester.reset([{"observations":"evidence", "uncertainties":[]}])
    agent.plugin_manager.register("ModelRequester", VisionRequester, activate=False)
    agent.set_settings("vlm", {"provider":"VisionRequester", "model":"eyes"})
    old = VisionRequester.request_model
    attempts = []
    async def dispatch(self, data):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ReadError("synthetic disconnect")
        async for item in old(self, data):
            yield item
    # Both attempts construct a request; this fixture counts construction separately.
    VisionRequester.responses *= 2
    monkeypatch.setattr(VisionRequester, "request_model", dispatch)
    execution = image(agent.input(file=AudioInput(b"audio"), type="audio"))
    assert execution.get_data(max_retries=1) == "final"
    assert len(attempts) == 2
    assert [call[0] for call in AudioDriver.calls] == ["stt"]
    assert execution.get_meta().get("media", {})["retries_used"] == 1


def test_invalid_later_media_does_not_consume_stt(tmp_path):
    from agently.types.data.audio import AudioInput
    agent = audio_agent(tmp_path, [])
    agent.set_settings("vlm", {"provider": "VisionRequester", "model": "eyes", "vision": False})
    agent.plugin_manager.register("ModelRequester", VisionRequester, activate=False)
    with pytest.raises(ValueError, match="disables vision"):
        image(agent.input(file=AudioInput(b"audio"), type="audio")).get_data()
    assert not AudioDriver.calls


def test_mixed_ocr_preserves_image_identity_and_question(tmp_path):
    agent = make_agent(tmp_path, ["final"], [{"observations":"first", "uncertainties":[]}])
    OCRRequester.reset(["second text"])
    agent.plugin_manager.register("ModelRequester", OCRRequester, activate=False)
    agent.set_settings("ocr", {"provider":"OCRRequester", "model":"ocr"})
    assert image(image(agent), mode="ocr", question="Read the label").get_data() == "final"
    info = str(ScriptedExecutionRequester.requests[0]["info"])
    assert "'image': 2" in info and "Read the label" in info


@pytest.mark.asyncio
async def test_stream_say_final_and_no_implicit_replay(tmp_path):
    agent = audio_agent(tmp_path, ["final answer."])
    execution = agent.input("question")
    async with execution.stream_say() as stream:
        chunks = [chunk async for chunk in stream]
    assert len(chunks) == 1 and chunks[0].data == b"synthetic-audio"
    assert await execution.async_get_text() == "final answer."
    with pytest.raises(RuntimeError, match="already consumed"):
        async with execution.stream_say():
            pass
    assert len(AudioDriver.calls) == 1


@pytest.mark.asyncio
async def test_concurrent_say_reuses_one_audio_request(tmp_path):
    import asyncio
    agent = audio_agent(tmp_path, ["answer"])
    execution = agent.input("question")
    first, second = await asyncio.gather(execution.async_say(), execution.async_say())
    assert first is second and len(AudioDriver.calls) == 1


@pytest.mark.asyncio
async def test_speech_failure_keeps_text_and_is_not_replayed(tmp_path, monkeypatch):
    agent = audio_agent(tmp_path, ["answer"])
    calls = []
    async def fail(self, request):
        calls.append(request)
        raise RuntimeError("uncertain audio delivery")
    monkeypatch.setattr(AudioDriver, "tts", fail)
    execution = agent.input("question")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="uncertain audio"):
            await execution.async_say()
    assert await execution.async_get_text() == "answer" and len(calls) == 1


@pytest.mark.asyncio
async def test_cancel_vision_settles_child_and_skips_final(tmp_path, monkeypatch):
    import asyncio
    entered, settled = asyncio.Event(), asyncio.Event()
    agent = make_agent(tmp_path, [], ["unused"])
    async def hanging(self, data):
        entered.set()
        try:
            await asyncio.Event().wait()
            yield "response", "never"
        finally:
            settled.set()
    monkeypatch.setattr(VisionRequester, "request_model", hanging)
    execution = image(agent)
    task = asyncio.create_task(execution.async_get_data())
    await asyncio.wait_for(entered.wait(), 3)
    await execution.async_cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(settled.wait(), 3)
    assert ScriptedExecutionRequester.model_dispatches == 0


def test_pending_audio_cannot_claim_snapshot_support(tmp_path):
    from agently.types.data.audio import AudioInput
    execution = audio_agent(tmp_path, []).input(file=AudioInput(b"input"), type="audio")
    assert execution.control_capabilities["snapshot_boundaries"] == []
    with pytest.raises(NotImplementedError, match="rebinding"):
        execution.save()


def test_explicit_request_key_wins_llm_default(tmp_path):
    agent = make_agent(tmp_path, ["answer"], [], roles="llm")
    agent.set_settings("model_pool", {"explicit": {"provider":"ScriptedExecutionRequester", "model":"explicit-model"}})
    request = agent.create_request(model_key="explicit").input("question")
    assert request.get_data() == "answer"
    assert request.settings.get("plugins.ModelRequester.ScriptedExecutionRequester.model") == "explicit-model"


@pytest.mark.asyncio
async def test_rework_reuses_media_but_speaks_new_revision(tmp_path):
    from agently.types.data.audio import AudioInput
    agent = audio_agent(tmp_path, ["first answer", "revised answer"])
    execution = agent.input(file=AudioInput(b"audio"), type="audio")
    await execution.async_say()
    assert await execution.async_rework("Make it clearer") == "revised answer"
    await execution.async_say()
    assert [call[0] for call in AudioDriver.calls] == ["stt", "tts", "tts"]
    assert AudioDriver.calls[-1][1].text == "revised answer"


def test_media_evidence_reaches_system_one_and_final_model(tmp_path):
    from agently import Probability
    from test_system_one import SmallRequester
    agent = make_agent(tmp_path, [{"field_0":"summary"}], [{"observations":"visible evidence", "uncertainties":[]}])
    SmallRequester.reset([{"field_0":0.8}])
    SmallRequester.configurations = []
    agent.plugin_manager.register("ModelRequester", SmallRequester, activate=False)
    agent.set_settings("system_one", {"provider":"SmallRequester", "model":"fast"})
    output = image(agent).output({"p":Probability("Supported?"), "summary":str}).get_data()
    assert output == {"p":0.8, "summary":"summary"}
    assert "visible evidence" in str(SmallRequester.requests[0].get("info"))
    assert "visible evidence" in str(ScriptedExecutionRequester.requests[0].get("info"))


def test_vlm_can_answer_text_without_empty_visual_stage(tmp_path):
    agent = make_agent(tmp_path, [], ["answer"], roles="vlm")
    execution = agent.input("text question")
    assert execution.get_data() == "answer"
    assert execution.get_meta().get("media", {}).get("stages") == []
    assert VisionRequester.model_dispatches == 1


@pytest.mark.parametrize("limit", [1, 2])
def test_visual_stage_shares_execution_model_budget(tmp_path, limit):
    from agently.core.application.AgentExecution import AgentExecutionLimitExceeded
    agent = make_agent(tmp_path, ["final"], [{"observations":"evidence", "uncertainties":[]}])
    execution = image(agent.create_execution(limits={"max_model_requests":limit}))
    if limit == 1:
        with pytest.raises(AgentExecutionLimitExceeded):
            execution.get_data(max_retries=0)
        assert ScriptedExecutionRequester.model_dispatches == 0
    else:
        assert execution.get_data() == "final"
        assert execution.execution_context.model_request_count == 2


def test_media_preflight_does_not_advance_credential_selection(tmp_path, monkeypatch):
    import importlib
    pools = importlib.import_module("agently.utils.ModelPool")
    resolve = pools.resolve_model_pool_settings
    selections = []
    def select(key, settings):
        selections.append(key)
        return resolve(key, settings)
    monkeypatch.setattr(pools, "resolve_model_pool_settings", select)
    agent = make_agent(tmp_path, ["final"], [{"observations":"evidence", "uncertainties":[]}])
    assert image(agent).get_data() == "final"
    assert len(selections) == 2  # one resolved profile for each actual model stage


@pytest.mark.asyncio
async def test_partial_speech_close_cancels_owned_execution(tmp_path, monkeypatch):
    import asyncio
    from agently import TextSegmentOptions
    agent = audio_agent(tmp_path, ["unused"])
    entered, settled = asyncio.Event(), asyncio.Event()
    async def pending(self, data):
        entered.set()
        try:
            await asyncio.Event().wait()
            yield "response", "unused"
        finally:
            settled.set()
    monkeypatch.setattr(ScriptedExecutionRequester, "request_model", pending)
    execution = agent.input("question")
    await execution.emit_stream("progress", {"message":"Public progress is ready. More work is pending."}, meta={"stream_kind":"progress"})
    async with execution.stream_say(scope="all", segments=TextSegmentOptions(expect_chars=10, grace_chars=20)) as stream:
        first = await asyncio.wait_for(anext(stream), 3)
        assert first.data == b"synthetic-audio"
        await asyncio.wait_for(entered.wait(), 3)
    await asyncio.wait_for(settled.wait(), 3)
    assert getattr(execution, "_cancel_requested") is True


def test_direct_ocr_can_disable_shared_retries(tmp_path, monkeypatch):
    import httpx
    agent = make_agent(tmp_path, [], [], roles="llm")
    OCRRequester.reset(["unused"])
    agent.plugin_manager.register("ModelRequester", OCRRequester, activate=False)
    agent.set_settings("ocr", {"provider":"OCRRequester", "model":"ocr"})
    attempts = []
    async def disconnect(self, data):
        attempts.append(data)
        raise httpx.ReadError("protocol fixture disconnect")
        yield "response", "unreachable"
    monkeypatch.setattr(OCRRequester, "request_model", disconnect)
    with pytest.raises(httpx.ReadError):
        image(agent, mode="ocr").to_text(max_retries=0)
    assert len(attempts) == 1
