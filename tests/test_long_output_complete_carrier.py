"""A provider stop is not proof that the entire JSON carrier is valid."""
import pytest
from pydantic import BaseModel, Field

from agently.utils import StreamingJSONParser
from test_builtin_agent_executions import create_execution_agent, ScriptedExecutionRequester


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [
    '{"body":"first"}\n{"body":"lost"}',
    '{"body":"first"}\n</think>',
    '{"body":"unfinished',
    '{"body":"first","body":"overwritten"}',
])
async def test_complete_provider_does_not_authorize_partial_json(tmp_path, raw):
    agent=create_execution_agent(tmp_path,'raw-carrier',[raw])
    execution=agent.create_execution().input('Write.').output({'body':str}).auto_continue()
    with pytest.raises(Exception,match='complete JSON carrier'):
        await execution.async_get_data(max_retries=0)
    assert ScriptedExecutionRequester.model_dispatches==1


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', ['{"body":"done"}', '```json\n{"body":"done"}\n```'])
async def test_valid_complete_json_still_uses_one_request(tmp_path,raw):
    agent=create_execution_agent(tmp_path,'valid-carrier',[raw])
    execution=agent.create_execution().input('Write.').output({'body':str}).auto_continue()
    assert await execution.async_get_data(max_retries=0)=={'body':'done'}
    assert ScriptedExecutionRequester.model_dispatches==1


@pytest.mark.parametrize('raw', ['1','-2','1.25','1e3'])
def test_numeric_root_needs_explicit_end_evidence(raw):
    assert not StreamingJSONParser._inspect_json_prefix(raw).root_complete
    assert StreamingJSONParser._inspect_json_prefix(raw,terminal_complete=True).root_complete


@pytest.mark.asyncio
async def test_validation_replacement_cannot_discard_its_tail(tmp_path):
    class Body(BaseModel):
        body: str = Field(min_length=3)
    agent=create_execution_agent(tmp_path,'replacement-carrier',[
        '{"body":"x"}', '{"body":"valid"}\n{"body":"lost"}'])
    execution=agent.create_execution().input('Write.').output(Body).auto_continue()
    with pytest.raises(Exception,match='complete JSON carrier'):
        await execution.async_get_data(max_retries=1)
    assert ScriptedExecutionRequester.model_dispatches==2


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement_terminal', [{}, {'finish_reason': 'length'}])
async def test_internal_stage_replacement_uses_its_own_delivery_evidence(tmp_path, monkeypatch, replacement_terminal):
    from agently.builtins.plugins.AgentExecution import AgentExecution
    from agently.builtins.plugins.AgentExecution.modules.model_stage import run_model_stage

    broadcast = ScriptedExecutionRequester.broadcast_response

    async def terminal(self, generator):
        async for event, value in broadcast(self, generator):
            if event == 'meta' and ScriptedExecutionRequester.model_dispatches == 2:
                value = replacement_terminal
            yield event, value

    monkeypatch.setattr(ScriptedExecutionRequester, 'broadcast_response', terminal)

    class Body(BaseModel):
        body: str = Field(min_length=3)

    agent = create_execution_agent(tmp_path, 'internal-replacement', [{'body': 'x'}, {'body': 'valid'}])
    execution = agent.create_execution().input('Write.')
    assert isinstance(execution, AgentExecution)
    request = run_model_stage(
        execution, producer='long_content', stage='body', stage_input={}, stage_info={},
        stage_instructions=['Return the body.'], output=Body,
        read_task_context=False, ensure_long_output=True,
    )
    if replacement_terminal:
        with pytest.raises(RuntimeError, match='length-limited validation replacement'):
            await request
    else:
        assert (await request).value == {'body': 'valid'}
    assert ScriptedExecutionRequester.model_dispatches == 2


@pytest.mark.asyncio
async def test_internal_chapter_stage_cannot_discard_its_tail(tmp_path):
    agent=create_execution_agent(tmp_path,'chapter-carrier',[
        {'document_title':'Document','part_plan':[{'part_title':'Chapter','part_brief':'Write this chapter.'}]},
        '{"body":"first"}\n{"body":"lost"}'])
    with pytest.raises(Exception,match='complete JSON carrier'):
        await agent.create_execution('long_content').input('Write a document.').async_get_data()
    assert ScriptedExecutionRequester.model_dispatches==2


@pytest.mark.asyncio
async def test_replacement_length_cannot_borrow_initial_stop(tmp_path,monkeypatch):
    broadcast=ScriptedExecutionRequester.broadcast_response
    async def terminal(self,generator):
        async for event,value in broadcast(self,generator):
            if event=='meta' and ScriptedExecutionRequester.model_dispatches==2:
                assert isinstance(value,dict)
                value={**value,'finish_reason':'length'}
            yield event,value
    monkeypatch.setattr(ScriptedExecutionRequester,'broadcast_response',terminal)
    class Body(BaseModel):
        body: str = Field(min_length=3)
    agent=create_execution_agent(tmp_path,'replacement-length',[{'body':'x'},{'body':'valid'}])
    with pytest.raises(Exception,match='length-limited validation replacement'):
        await agent.create_execution().input('Write.').output(Body).auto_continue().async_get_data(max_retries=1)
    assert ScriptedExecutionRequester.model_dispatches==2
