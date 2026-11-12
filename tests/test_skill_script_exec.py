from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from agently import Agently
from agently.builtins.plugins.ActionExecutor.CodeExecutionActionExecutor import CodeExecutionActionExecutor
from agently.builtins.plugins.AgentExecution.modules.execution import (
    AgentExecution as ConcreteAgentExecution,
)
from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
    DockerExecutionResource,
)
from agently.core.application.SkillLibrary import SkillBinding
from agently.core.runtime import bind_runtime_context
from agently.types.data import SkillScriptAuthorization


def _write_skill(
    root: Path,
    *,
    trust_marker: str = "ok",
    name: str = "Script Bridge",
) -> Path:
    (root / "scripts").mkdir(parents=True)
    (root / "references").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Validate an artifact.\n---\n\n"
        "Use `scripts/check.py` through the available Skill script exec Action.",
        encoding="utf-8",
    )
    (root / "scripts" / "check.py").write_text(
        "from pathlib import Path\n"
        f"Path('output/validated.txt').write_text('{trust_marker}')\n",
        encoding="utf-8",
    )
    (root / "scripts" / "other.py").write_text(
        "print('other')\n",
        encoding="utf-8",
    )
    (root / "references" / "notes.md").write_text("not executable", encoding="utf-8")
    return root


@pytest.mark.asyncio
async def test_exec_registers_one_action_for_all_bound_python_scripts(tmp_path: Path) -> None:
    agent = Agently.create_agent("skill-script-one-action")
    package = agent.skill_library.install(
        _write_skill(tmp_path / "skill"),
        trust="trusted",
    )
    execution = agent.create_execution().require_skills([package.revision_ref])
    await execution.async_prepare_task_context()

    action_id = agent.enable_skill_script_exec(
        execution,
        authorization=SkillScriptAuthorization(auto_allow=True),
        language="python",
    )

    assert action_id == "exec_skill_python"
    assert cast(ConcreteAgentExecution, execution)._task_context_prepared is True
    assert execution.local_action_ids == [action_id]
    spec = execution.action.action_registry.get_spec(action_id)
    assert spec is not None
    assert "kwargs" in spec
    assert "side_effect_level" in spec
    assert "sandbox_required" in spec
    assert "meta" in spec
    assert spec["kwargs"] == {
        "script_path": (str, "Relative executable script path.", True),
        "args": ("list[str]", "Optional bounded script arguments."),
    }
    assert spec["side_effect_level"] == "exec"
    assert spec["sandbox_required"] is True
    assert spec["meta"]["component"] == "skill_script_exec"
    assert spec["meta"]["language"] == "python"
    assert callable(spec["meta"]["_execution_resource_requirements_factory"])
    assert "tags" in spec
    assert f"agent-{agent.name}" not in spec["tags"]
    assert "skill_revision_ref" not in spec["meta"]
    assert "installed_path" not in spec["meta"]


def test_exec_rejects_unprepared_untrusted_or_unauthorized_scope(tmp_path: Path) -> None:
    agent = Agently.create_agent("skill-script-rejections")
    trusted = agent.skill_library.install(
        _write_skill(tmp_path / "trusted"),
        trust="trusted",
    )
    untrusted = agent.skill_library.install(
        _write_skill(tmp_path / "untrusted", trust_marker="untrusted"),
        trust="untrusted",
    )
    execution = cast(ConcreteAgentExecution, agent.create_execution())

    with pytest.raises(PermissionError, match="authorization|auto_allow"):
        agent.enable_skill_script_exec(
            execution,
            authorization=SkillScriptAuthorization(auto_allow=False),
        )
    with pytest.raises(RuntimeError, match="Prepare"):
        agent.enable_skill_script_exec(
            execution,
            authorization=SkillScriptAuthorization(auto_allow=True),
        )

    execution.skill_bindings = [
        SkillBinding(
            binding_id="untrusted-binding",
            task_id=execution.id,
            canonical_ref=untrusted.canonical_ref,
            revision=untrusted.revision,
            revision_ref=untrusted.revision_ref,
            mode="required",
        )
    ]
    cast(ConcreteAgentExecution, execution)._task_context_prepared = True
    with pytest.raises(PermissionError, match="trusted exact"):
        agent.enable_skill_script_exec(
            execution,
            authorization=SkillScriptAuthorization(auto_allow=True),
        )

    execution.skill_bindings = [
        SkillBinding.create(trusted, task_id="another-execution", mode="required")
    ]
    with pytest.raises(PermissionError, match="another task"):
        agent.enable_skill_script_exec(
            execution,
            authorization=SkillScriptAuthorization(auto_allow=True),
        )


@pytest.mark.asyncio
async def test_exec_rejects_conflicting_stable_action_id_without_replacement(
    tmp_path: Path,
) -> None:
    agent = Agently.create_agent("skill-script-action-conflict")
    package = agent.skill_library.install(
        _write_skill(tmp_path / "skill"),
        trust="trusted",
    )
    agent.action.register_action(
        action_id="exec_skill_python",
        desc="Application-owned action with a conflicting id.",
        kwargs={},
        func=lambda: "application-owned",
    )
    original_spec = agent.action.action_registry.get_spec("exec_skill_python")
    original_executor = agent.action.action_registry.get_executor("exec_skill_python")
    execution = agent.create_execution().require_skills(package.revision_ref)
    await execution.async_prepare_task_context()

    with pytest.raises(ValueError, match="already registered by another owner"):
        agent.enable_skill_script_exec(
            execution,
            authorization=SkillScriptAuthorization(auto_allow=True),
        )

    assert agent.action.action_registry.get_spec("exec_skill_python") == original_spec
    assert (
        agent.action.action_registry.get_executor("exec_skill_python")
        is original_executor
    )
    assert execution.local_action_ids == []
    assert execution.execution_context.get_skill_script_exec_authorization(
        "exec_skill_python"
    ) is None


@pytest.mark.asyncio
async def test_exec_preserves_prepared_scope_and_does_not_repeat_skill_selection(
    tmp_path: Path,
) -> None:
    agent = Agently.create_agent("skill-script-no-reselection")
    package = agent.skill_library.install(
        _write_skill(tmp_path / "skill"),
        trust="trusted",
    )

    class SelectionRequest:
        def __init__(self) -> None:
            self.call_count = 0
            self.cards: list[dict[str, Any]] = []

        def input(self, _value: Any) -> "SelectionRequest":
            return self

        def info(self, value: Any) -> "SelectionRequest":
            self.cards = value["offered_skills"]
            return self

        def instruct(self, _value: Any) -> "SelectionRequest":
            return self

        def output(self, _value: Any, *, format: str | None = None) -> "SelectionRequest":
            assert format == "json"
            return self

        async def async_get_data(self) -> dict[str, Any]:
            self.call_count += 1
            return {"selected_keys": [self.cards[0]["skill_key"]]}

    selector = SelectionRequest()
    cast(Any, agent).create_temp_request = lambda: selector
    agent.use_skills(package.skill_id, always=True)
    execution = agent.create_execution().input("Run the validation script")

    await execution.async_prepare_task_context()
    frozen_bindings = tuple(execution.skill_bindings)
    agent.enable_skill_script_exec(
        execution,
        authorization=SkillScriptAuthorization(auto_allow=True),
    )
    await execution.async_prepare_task_context()

    assert selector.call_count == 1
    assert cast(ConcreteAgentExecution, execution)._task_context_prepared is True
    assert tuple(execution.skill_bindings) == frozen_bindings


@pytest.mark.asyncio
async def test_later_execution_reuses_stable_action_without_scope_or_permission_leak(
    tmp_path: Path,
) -> None:
    agent = Agently.create_agent("skill-script-later-request")
    package = agent.skill_library.install(
        _write_skill(tmp_path / "skill"),
        trust="trusted",
    )
    agent.use_skills(package.skill_id, always=True)

    class PerRequestSelection:
        def __init__(self) -> None:
            self.task = ""
            self.cards: list[dict[str, Any]] = []
            self.call_count = 0

        def input(self, value: Any) -> "PerRequestSelection":
            self.task = str(value["input"])
            return self

        def info(self, value: Any) -> "PerRequestSelection":
            self.cards = value["offered_skills"]
            return self

        def instruct(self, _value: Any) -> "PerRequestSelection":
            return self

        def output(self, _value: Any, *, format: str | None = None) -> "PerRequestSelection":
            assert format == "json"
            return self

        async def async_get_data(self) -> dict[str, Any]:
            self.call_count += 1
            selected = [self.cards[0]["skill_key"]] if "script" in self.task else []
            return {"selected_keys": selected}

    requests: list[PerRequestSelection] = []

    def create_selection_request() -> PerRequestSelection:
        request = PerRequestSelection()
        requests.append(request)
        return request

    cast(Any, agent).create_temp_request = create_selection_request
    first = agent.create_execution().input("Explain the release status")
    await first.async_prepare_task_context()
    assert first.skill_bindings == []

    later = agent.create_execution().input("Run the release script")
    await later.async_prepare_task_context()
    action_id = agent.enable_skill_script_exec(
        later,
        authorization=SkillScriptAuthorization(auto_allow=True),
    )

    third = agent.create_execution().input("Run the script again")
    await third.async_prepare_task_context()
    third_action_id = agent.enable_skill_script_exec(
        third,
        authorization=SkillScriptAuthorization(auto_allow=True),
    )

    assert first.id != later.id != third.id
    assert len(requests) == 3
    assert [request.call_count for request in requests] == [1, 1, 1]
    assert action_id == third_action_id == "exec_skill_python"
    registered = [
        spec
        for spec in agent.action.get_action_list()
        if spec.get("meta", {}).get("component") == "skill_script_exec"
    ]
    assert len(registered) == 1

    def visible_ids(execution: Any) -> set[str]:
        with bind_runtime_context(agent_execution_context=execution.execution_context):
            return {
                str(item.get("action_id"))
                for item in agent._get_scoped_action_list()
            }

    assert action_id not in visible_ids(first)
    assert action_id in visible_ids(later)
    assert action_id in visible_ids(third)
    assert first.execution_context.get_skill_script_exec_authorization(action_id) is None


@pytest.mark.asyncio
async def test_reconfiguring_prepared_scope_revokes_dependent_action(
    tmp_path: Path,
) -> None:
    agent = Agently.create_agent("skill-script-reconfigure")
    package = agent.skill_library.install(
        _write_skill(tmp_path / "skill"),
        trust="trusted",
    )
    execution = agent.create_execution().require_skills(package.skill_id)
    await execution.async_prepare_task_context()
    action_id = agent.enable_skill_script_exec(
        execution,
        authorization=SkillScriptAuthorization(auto_allow=True),
    )

    execution.input("A changed request must be prepared again")

    assert cast(ConcreteAgentExecution, execution)._task_context_prepared is False
    assert action_id not in execution.local_action_ids
    assert execution.execution_context.get_skill_script_exec_authorization(action_id) is None

    execution.require_skills(package.skill_id)
    await execution.async_prepare_task_context()
    action_id = agent.enable_skill_script_exec(
        execution,
        authorization=SkillScriptAuthorization(auto_allow=True),
    )
    previous_context = execution.execution_context

    execution.create_execution(options={"effort": "low"})

    assert execution.execution_context is not previous_context
    assert cast(ConcreteAgentExecution, execution)._task_context_prepared is False
    assert action_id not in execution.local_action_ids
    assert previous_context.get_skill_script_exec_authorization(action_id) is None
    assert execution.execution_context.get_skill_script_exec_authorization(action_id) is None


@pytest.mark.asyncio
async def test_started_or_foreign_execution_rejection_has_no_registry_side_effect(
    tmp_path: Path,
) -> None:
    owner = Agently.create_agent("skill-script-owner")
    foreign = Agently.create_agent("skill-script-foreign")
    package = owner.skill_library.install(
        _write_skill(tmp_path / "skill"),
        trust="trusted",
    )
    execution = cast(
        ConcreteAgentExecution, owner.create_execution().require_skills(package.skill_id)
    )
    await execution.async_prepare_task_context()

    before = set(owner.action.action_registry.list_action_ids())
    execution._started = True
    with pytest.raises(RuntimeError, match="already started"):
        owner.enable_skill_script_exec(
            execution,
            authorization=SkillScriptAuthorization(auto_allow=True),
        )
    assert set(owner.action.action_registry.list_action_ids()) == before

    execution._started = False
    foreign_before = set(foreign.action.action_registry.list_action_ids())
    with pytest.raises(PermissionError, match="Agent owning"):
        foreign.enable_skill_script_exec(
            execution,
            authorization=SkillScriptAuthorization(auto_allow=True),
        )
    assert set(foreign.action.action_registry.list_action_ids()) == foreign_before


@pytest.mark.asyncio
async def test_stable_action_resolves_concurrent_execution_authorizations(
    tmp_path: Path,
) -> None:
    agent = Agently.create_agent("skill-script-concurrent")
    first_package = agent.skill_library.install(
        _write_skill(tmp_path / "first", trust_marker="first", name="First Script"),
        trust="trusted",
    )
    second_package = agent.skill_library.install(
        _write_skill(tmp_path / "second", trust_marker="second", name="Second Script"),
        trust="trusted",
    )
    first = agent.create_execution().require_skills(first_package.revision_ref)
    second = agent.create_execution().require_skills(second_package.revision_ref)
    await asyncio.gather(
        first.async_prepare_task_context(),
        second.async_prepare_task_context(),
    )
    action_id = agent.enable_skill_script_exec(
        first,
        authorization=SkillScriptAuthorization(
            auto_allow=True,
            expected_outputs=("output/first.txt",),
        ),
    )
    assert agent.enable_skill_script_exec(
        second,
        authorization=SkillScriptAuthorization(
            auto_allow=True,
            expected_outputs=("output/second.txt",),
        ),
    ) == action_id
    spec = agent.action.action_registry.get_spec(action_id)
    executor = agent.action.action_registry.get_executor(action_id)
    assert spec is not None and isinstance(executor, CodeExecutionActionExecutor)
    executor_spec = dict(spec)
    spec_meta = spec.get("meta", {})

    async def resolve(execution: Any) -> tuple[str, list[str]]:
        with bind_runtime_context(agent_execution_context=execution.execution_context):
            await asyncio.sleep(0)
            request = executor._request_from_action(
                spec=executor_spec,
                action_call={"action_input": {"script_path": "scripts/check.py"}},
            )
            factory = spec_meta["_execution_resource_requirements_factory"]
            requirements = factory(
                spec=spec,
                settings=execution.request.settings,
                policy={},
            )
            outputs = requirements[0]["workspace_access"]["expected_outputs"]
            return request.provenance["revision_ref"], outputs

    first_result, second_result = await asyncio.gather(resolve(first), resolve(second))
    assert first_result == (first_package.revision_ref, ["output/first.txt"])
    assert second_result == (second_package.revision_ref, ["output/second.txt"])

    with pytest.raises(PermissionError, match="current AgentExecution"):
        executor._request_from_action(
            spec=executor_spec,
            action_call={"action_input": {"script_path": "scripts/check.py"}},
        )
    unauthorized_result = await agent.action.async_execute_action(
        action_id,
        {"script_path": "scripts/check.py"},
    )
    assert unauthorized_result.get("status") == "error"
    assert "not authorized for the current AgentExecution" in str(
        unauthorized_result.get("error")
    )


@pytest.mark.asyncio
async def test_agent_enables_exec_and_resolves_script_at_call_time(tmp_path: Path) -> None:
    agent = Agently.create_agent("skill-script-application-api").use_task_workspace(
        tmp_path / "workspace",
        mode="read_only",
    )
    package = agent.skill_library.install(
        _write_skill(tmp_path / "skill"),
        trust="trusted",
    )
    execution = agent.create_execution().require_skills([package.revision_ref])
    await execution.async_prepare_task_context()

    enabled = agent.enable_skill_script_exec(
        execution,
        authorization=SkillScriptAuthorization(auto_allow=True),
    )
    executor = execution.action.action_registry.get_executor(enabled)
    assert isinstance(executor, CodeExecutionActionExecutor)
    spec = execution.action.action_registry.get_spec(enabled)
    assert spec is not None
    assert "tags" in spec
    assert f"agent-{agent.name}" not in spec["tags"]
    assert enabled in (execution.execution_context.scoped_action_ids() or set())
    with bind_runtime_context(agent_execution_context=execution.execution_context):
        request = executor._request_from_action(
            spec=dict(spec),
            action_call={
                "action_input": {
                    "script_path": "scripts/check.py",
                    "args": [],
                }
            },
        )

    assert request.entrypoint == "scripts/check.py"
    assert request.provenance["binding_id"] == execution.skill_bindings[0].binding_id
    assert request.provenance["revision_ref"] == package.revision_ref
    assert request.provenance["resource_path"] == "scripts/check.py"
    assert request.provenance["resource_sha256"] == package.resource(
        "scripts/check.py"
    ).sha256

    with bind_runtime_context(agent_execution_context=execution.execution_context):
        with pytest.raises(PermissionError, match="one executable resource"):
            executor._request_from_action(
                spec=dict(spec),
                action_call={
                    "action_input": {
                        "script_path": "scripts/not-bound.py",
                    }
                },
            )
        with pytest.raises(PermissionError, match="not an executable"):
            executor._request_from_action(
                spec=dict(spec),
                action_call={
                    "action_input": {
                        "script_path": "references/notes.md",
                    }
                },
            )


@pytest.mark.asyncio
async def test_exec_rejects_language_mismatch_and_digest_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agently.create_agent("skill-script-validation")
    package = agent.skill_library.install(
        _write_skill(tmp_path / "skill"),
        trust="trusted",
    )
    execution = agent.create_execution().require_skills([package.revision_ref])
    await execution.async_prepare_task_context()
    enabled = agent.enable_skill_script_exec(
        execution,
        authorization=SkillScriptAuthorization(auto_allow=True),
    )
    executor = execution.action.action_registry.get_executor(enabled)
    spec = execution.action.action_registry.get_spec(enabled)
    assert isinstance(executor, CodeExecutionActionExecutor) and spec is not None

    monkeypatch.setitem(executor._SKILL_SCRIPT_LANGUAGES, ".py", "nodejs")
    with bind_runtime_context(agent_execution_context=execution.execution_context):
        with pytest.raises(ValueError, match="requires 'nodejs'"):
            executor._request_from_action(
                spec=dict(spec),
                action_call={
                    "action_input": {
                        "script_path": "scripts/check.py",
                    }
                },
            )
    monkeypatch.setitem(executor._SKILL_SCRIPT_LANGUAGES, ".py", "python")

    original_read = agent.skill_library.read_resource

    def corrupted_read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        return replace(result, data=b"print('corrupted')\n")

    monkeypatch.setattr(agent.skill_library, "read_resource", corrupted_read)
    with bind_runtime_context(agent_execution_context=execution.execution_context):
        with pytest.raises(ValueError, match="digest"):
            executor._request_from_action(
                spec=dict(spec),
                action_call={
                    "action_input": {
                        "script_path": "scripts/check.py",
                    }
                },
            )


@pytest.mark.asyncio
async def test_skill_script_executes_through_workspace_and_docker(
    tmp_path: Path,
) -> None:
    docker_resource = DockerExecutionResource()
    availability = docker_resource.inspect_availability()
    if not availability["available"]:
        pytest.skip(f"Docker is unavailable: {availability}")
    image = DockerExecutionResource._default_image("python")
    if not docker_resource.inspect_image(image).get("exists"):
        pytest.skip(f"required local Docker image is unavailable: {image}")

    skill_root = _write_skill(tmp_path / "skill", trust_marker="actual-docker")
    (skill_root / "scripts" / "check.py").write_text(
        "from pathlib import Path\n"
        "Path('../output/validated.txt').write_text('actual-docker')\n"
        "print('skill-exec-actual')\n",
        encoding="utf-8",
    )
    agent = Agently.create_agent("skill-script-actual").use_task_workspace(
        tmp_path / "workspace",
        mode="read_only",
    )
    package = agent.skill_library.install(skill_root, trust="trusted")
    execution = agent.create_execution().require_skills([package.revision_ref])
    await execution.async_prepare_task_context()
    enabled = agent.enable_skill_script_exec(
        execution,
        authorization=SkillScriptAuthorization(
            auto_allow=True,
            expected_outputs=("output/validated.txt",),
        ),
    )

    with bind_runtime_context(agent_execution_context=execution.execution_context):
        result = await agent.action.async_execute_action(
            enabled,
            {
                "script_path": "scripts/check.py",
                "args": [],
            },
        )

    assert result.get("status") == "success"
    result_data = result.get("data")
    artifacts = result.get("artifacts")
    assert isinstance(result_data, dict)
    assert isinstance(artifacts, list) and len(artifacts) == 1
    assert result_data["stdout"] == "skill-exec-actual\n"
    published_path = artifacts[0].get("path", "")
    assert published_path.endswith("/output/validated.txt")
    assert published_path.startswith(".agently/files/")
    readback = await execution.task_workspace.read_file(published_path)
    assert readback.content == "actual-docker"
    assert result_data["meta"]["provider_contract"] == (
        "workspace_code_execution_v1"
    )
    assert result_data["meta"]["provenance"]["revision_ref"] == package.revision_ref
    assert result_data["meta"]["provenance"]["resource_path"] == "scripts/check.py"


@pytest.mark.asyncio
async def test_exec_fails_closed_when_script_path_is_ambiguous(
    tmp_path: Path,
) -> None:
    agent = Agently.create_agent("skill-script-ambiguous-path")
    first = agent.skill_library.install(
        _write_skill(tmp_path / "first", name="First Script Bridge"),
        trust="trusted",
    )
    second = agent.skill_library.install(
        _write_skill(tmp_path / "second", name="Second Script Bridge"),
        trust="trusted",
    )
    execution = agent.create_execution().require_skills(
        [first.revision_ref, second.revision_ref]
    )
    await execution.async_prepare_task_context()
    action_id = agent.enable_skill_script_exec(
        execution,
        authorization=SkillScriptAuthorization(auto_allow=True),
    )
    executor = execution.action.action_registry.get_executor(action_id)
    spec = execution.action.action_registry.get_spec(action_id)
    assert isinstance(executor, CodeExecutionActionExecutor) and spec is not None

    with bind_runtime_context(agent_execution_context=execution.execution_context):
        with pytest.raises(PermissionError, match="narrow the Skill scope"):
            executor._request_from_action(
                spec=dict(spec),
                action_call={"action_input": {"script_path": "scripts/check.py"}},
            )
