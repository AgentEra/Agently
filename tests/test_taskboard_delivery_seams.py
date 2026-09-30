"""Native context/file protocol regressions, not model-quality evidence."""
from types import SimpleNamespace

import pytest

from agently import Agently
from agently.core import AgentTask
from agently.types.data import TaskBoardRevision
from agently.core.context import ContextSelection
from agently.builtins.plugins.AgentExecution.long_task.BlockCarrier import WorkUnitResult


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import httpx

    async def forbidden(*args, **kwargs):
        raise AssertionError('Protocol tests must not dispatch HTTP requests')

    monkeypatch.setattr(httpx.AsyncClient, 'send', forbidden)


def context_for(task, *, shape='control', metadata=None):
    revision = TaskBoardRevision.from_value({
        'board_id': task.id, 'revision_id': 'rev-1',
        'graph': {'graph_id': 'seams', 'cards': [{
            'id': 'deliver', 'objective': 'Read the supplied document and return the answer.',
            'allowed_execution_shape': shape, 'metadata': metadata or {},
        }]},
    })
    return SimpleNamespace(card=revision.graph.cards[0], revision=revision,
                           dependency_results={}, planning_policy=None)


@pytest.mark.asyncio
@pytest.mark.parametrize('role,exists,missing_extra', [
    ('evidence_snippet', True, False), ('locator_ref', True, False),
    ('evidence_snippet', False, False), ('evidence_snippet', True, True),
])
async def test_readback_executes_declared_context_reads(tmp_path, monkeypatch, role, exists, missing_extra):
    if exists:
        (tmp_path / 'source.txt').write_text('Recorded source body.')
    task = AgentTask(Agently.create_agent().use_task_workspace(tmp_path),
                     goal='Read source.txt.', success_criteria=['Read source.'], execution='taskboard')
    class Selector:
        async def async_select(self, *, candidates, **kwargs):
            return ContextSelection(selected_keys=tuple(candidate.block_key for candidate in candidates))

    monkeypatch.setattr(task, '_task_context_semantic_selector', lambda: Selector())
    groups = [{
        'query': 'source.txt', 'source_kinds': ['task_workspace'], 'pattern': 'source.txt',
        'expected_role': role, 'max_results': 1,
    }]
    if missing_extra:
        groups.append({**groups[0], 'query': 'missing.txt', 'pattern': 'missing.txt'})
    context = context_for(task, shape='readback', metadata={'scoped_retrieval': {'query_groups': groups}})
    result = await task._run_taskboard_readback_card(context, {})
    assert result.status == ('completed' if exists else 'blocked')
    if missing_extra:
        assert result.preview['remaining_work']
    items = result.metadata['evidence_ledger']['items']
    snippets = [item for item in items if item.get('kind') == 'evidence_snippet']
    if exists and role == 'evidence_snippet':
        assert any(item.get('body') == 'Recorded source body.' and item['supports']['content'] for item in snippets)
    else:
        assert not any(item.get('supports', {}).get('content') for item in snippets)
    if exists:
        assert result.preview['scoped_retrieval_results']
        assert not any(item.get('code') == 'taskboard.readback.no_refs' for item in result.diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', ['control', 'model'])
@pytest.mark.parametrize('required_path', [False, True])
@pytest.mark.parametrize('body_field', ['candidate_final_result', 'artifact_markdown', 'final_result'])
async def test_first_card_text_stays_inline_unless_delivery_required(tmp_path, monkeypatch, shape, required_path, body_field):
    task = AgentTask(Agently.create_agent().use_task_workspace(tmp_path),
                     goal='Return a report.', success_criteria=['Report supplied.'], execution='taskboard')
    context = context_for(task, shape=shape)
    if required_path:
        task._taskboard_planned_task_workspace_deliverables = ['report.md']

    async def work(**kwargs):
        if shape == 'control':
            schema = kwargs['work_unit'].delivery_contract['execution_prompt']['output']
            assert 'candidate_final_result' in schema
            assert 'final_result' not in schema and 'artifact_markdown' not in schema
            assert ('artifact_manifest' in schema) is required_path
        return ({'status': 'completed', 'sufficient': True, 'next_board_action': 'finalize',
                 body_field: 'Complete report', 'artifact_manifest': {}, 'remaining_work': []},
                {'execution_id': 'synthetic-card', 'status': 'completed',
                 'logs': {'action_logs': [], 'route_logs': {}, 'errors': []}},
                WorkUnitResult(id=str(kwargs['work_unit'].id), status='completed'))

    monkeypatch.setattr(task, '_run_work_unit_through_blocks', work)
    run = task._run_taskboard_control_card if shape == 'control' else task._run_taskboard_card
    result = await run(context, {})
    assert result.status == 'completed'
    if required_path:
        assert result.file_refs
        assert task.task_workspace.resolve_file_path(
            'working/taskboard/deliver/terminal-candidates/report.md').read_text() == 'Complete report'
    else:
        assert not result.file_refs
        assert not list(tmp_path.rglob('*.md'))
        assert result.preview['candidate_final_result'] == 'Complete report'


@pytest.mark.asyncio
@pytest.mark.parametrize('stream,body', [(False, '# Final\n\nComplete body.'), (True, '# Final\n\nComplete body.'), (False, '')])
async def test_artifact_draft_consumes_one_completed_response(tmp_path, monkeypatch, stream, body):
    import json
    import httpx

    calls = []

    async def send(client, request, **kwargs):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload['stream'] is stream
        if stream:
            chunks = [body[:8], body[8:]]
            content = ''.join('data: ' + json.dumps({'choices': [{'delta': {'content': chunk}, 'finish_reason': None}]}) + '\n\n'
                              for chunk in chunks)
            content += 'data: [DONE]\n\n'
            return httpx.Response(200, request=request, content=content, headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, request=request, json={
            'id': 'synthetic-response', 'choices': [{'message': {'role': 'assistant', 'content': body}, 'finish_reason': 'stop'}],
        })

    monkeypatch.setattr(httpx.AsyncClient, 'send', send)
    agent = Agently.create_agent().use_task_workspace(tmp_path)
    agent.set_settings('plugins.ModelRequester.OpenAICompatible', {
        'base_url': 'http://fixture.invalid/v1', 'auth': 'synthetic-test-key', 'model': 'synthetic', 'stream': stream,
    })
    task = AgentTask(agent, goal='Write the document.', success_criteria=['Readable document.'])
    delivered = await task._stream_task_workspace_artifact_draft(
        path='report.md', plan={'deliverable_mode': 'task_workspace_artifact'},
        execution_result={'artifact_manifest': {'path': 'report.md'}},
        execution_meta={}, source='synthetic.protocol', context_pack={},
    )
    assert len(calls) == 1
    if body:
        assert delivered is not None, json.dumps(task.diagnostics.get('task_workspace_artifact_delivery'))
        assert delivered['status'] == 'delivered'
        ref = delivered['file_refs'][0]
        assert task.task_workspace.resolve_file_path(ref['path']).read_text() == body
    else:
        assert delivered is None
        assert task.diagnostics['task_workspace_artifact_delivery'][-1]['error']['type'] == 'EmptyWorkspaceArtifactDraft'
