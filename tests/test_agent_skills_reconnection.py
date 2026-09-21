from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, cast

import pytest

from agently import Agently
from agently.builtins.agent_extensions.SkillsExtension import SkillsExtension
from agently.core import SkillLibrary
from agently.types.data import ContextReadIntent


def _write_skill(root: Path, *, name: str, description: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"Apply the {name} procedure.",
        encoding="utf-8",
    )
    return root


class _SelectionRequest:
    def __init__(self, selected_keys: list[str]) -> None:
        self.selected_keys = selected_keys
        self.slots: dict[str, Any] = {}

    def input(self, value: Any) -> "_SelectionRequest":
        self.slots["input"] = value
        return self

    def info(self, value: Any) -> "_SelectionRequest":
        self.slots["info"] = value
        return self

    def instruct(self, value: Any) -> "_SelectionRequest":
        self.slots["instruct"] = value
        return self

    def output(self, value: Any, *, format: str | None = None) -> "_SelectionRequest":
        self.slots["output"] = value
        self.slots["output_format"] = format
        return self

    async def async_get_data(self) -> dict[str, Any]:
        return {"selected_keys": self.selected_keys}


class _ExecutionAwareSelectionRequest(_SelectionRequest):
    def __init__(self, selected_keys: list[str]) -> None:
        super().__init__(selected_keys)
        self.call_count = 0
        self.observed_execution_context: Any = None

    async def async_get_data(self) -> dict[str, Any]:
        from agently.core.runtime.RuntimeContext import (
            get_current_agent_execution_context,
        )

        self.call_count += 1
        self.observed_execution_context = get_current_agent_execution_context()
        return await super().async_get_data()


@pytest.mark.asyncio
async def test_skill_selection_receives_complete_task_without_replacing_its_output(
    tmp_path: Path,
) -> None:
    """Request delivery is a protocol assertion, not a semantic relevance test."""
    library = SkillLibrary(tmp_path / "library")
    package = library.install(
        _write_skill(tmp_path / "guide", name="Review", description="Review a report."),
        trust="trusted",
    )
    agent = Agently.create_agent("skill-complete-task").use_task_workspace(tmp_path / "work")
    agent.skill_library = library
    request = _ExecutionAwareSelectionRequest(["skill-option:1"])
    cast(Any, agent).create_temp_request = lambda: request
    execution = (
        agent.create_execution()
        .goal(["Analyze the report", "Prepare the external handoff"],
              success_criteria=["Remove personal details"], turn_on_long_task=False)
        .input({"source": "quarterly report"})
        .system({"audience": "external partners"})
        .info({"handling": "contact information is private"})
        .instruct("Deliver a reusable spreadsheet")
        .output({"workbook": (str, "Path to the completed workbook", True)})
        .use_skills(package.revision_ref)
    )
    # Use the authoritative draft, including direct pre-start Prompt writes.
    execution.request_prompt.set("info", {"handling": "redact contacts before delivery"})
    prompt = execution.request_prompt.get()
    assert isinstance(prompt, dict)
    original_prompt = dict(prompt)

    await execution.async_prepare_task_context()
    await execution.async_prepare_task_context()

    assert request.slots["input"] == {
        "goals": ["Analyze the report", "Prepare the external handoff"],
        "success_criteria": ["Remove personal details"],
        "input": {"source": "quarterly report"},
    }
    assert request.slots["info"]["task_context"] == {
        key: original_prompt[key] for key in ("system", "info", "instruct", "output")
    }
    assert list(request.slots["output"]) == ["selected_keys"]
    assert execution.request_prompt.get() == original_prompt
    assert request.call_count == 1


@pytest.mark.asyncio
async def test_skill_selection_preserves_input_when_no_goal_is_declared(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(
        _write_skill(tmp_path / "guide", name="Review", description="Review a report."),
        trust="trusted",
    )
    agent = Agently.create_agent("skill-input-only").use_task_workspace(tmp_path / "work")
    agent.skill_library = library
    request = _SelectionRequest([])
    cast(Any, agent).create_temp_request = lambda: request
    execution = agent.create_execution().input("Review the attached report").use_skills(package.revision_ref)

    await execution.async_prepare_task_context()

    assert request.slots["input"] == {
        "goals": [], "success_criteria": [], "input": "Review the attached report",
    }
    assert request.slots["info"]["task_context"] == {}
    assert execution.skill_bindings == []


@pytest.mark.asyncio
async def test_model_decision_skill_selection_uses_host_keys_and_exact_revision(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    first = library.install(
        _write_skill(tmp_path / "first", name="Release Review", description="Review a release."),
        trust="trusted",
    )
    second = library.install(
        _write_skill(tmp_path / "second", name="Email Draft", description="Draft an email."),
        trust="trusted",
    )
    agent = Agently.create_agent("skill-selection-test").use_task_workspace(tmp_path / "work")
    agent.skill_library = library
    request = _SelectionRequest(["skill-option:2"])
    cast(Any, agent).create_temp_request = lambda: request
    execution = (
        agent.create_execution()
        .input("Draft the customer email")
        .use_skills([first.skill_id, second.skill_id], mode="model_decision")
    )

    await execution.async_prepare_task_context()

    cards = request.slots["info"]["offered_skills"]
    assert [card["skill_key"] for card in cards] == ["skill-option:1", "skill-option:2"]
    assert cards[0]["name"] == first.name
    assert "revision_ref" not in cards[0]
    assert "installed_path" not in cards[0]
    assert request.slots["output_format"] == "json"
    assert [binding.revision_ref for binding in execution.skill_bindings] == [
        second.revision_ref
    ]
    assert execution.skill_bindings[0].mode == "model_decision"


@pytest.mark.asyncio
async def test_skill_selection_uses_execution_scope_and_unchanged_preparation_is_idempotent(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    first = library.install(
        _write_skill(tmp_path / "first", name="First Scope", description="First procedure."),
        trust="trusted",
    )
    second = library.install(
        _write_skill(tmp_path / "second", name="Second Scope", description="Second procedure."),
        trust="trusted",
    )
    agent = Agently.create_agent("skill-execution-scope-test").use_task_workspace(
        tmp_path / "work"
    )
    agent.skill_library = library
    agent.use_skills(first.skill_id, always=True)
    request = _ExecutionAwareSelectionRequest(["skill-option:1"])
    cast(Any, agent).create_temp_request = lambda: request
    execution = agent.create_execution().input("Apply the first procedure")

    first_context = await execution.async_prepare_task_context()
    agent.use_skills(second.skill_id, always=True)
    second_context = await execution.async_prepare_task_context()

    assert first_context is second_context
    assert request.call_count == 1
    assert request.observed_execution_context is execution.execution_context
    assert execution.diagnostics["skill_scope"] == {
        "status": "frozen",
        "required_revision_refs": [],
        "model_decision_revision_refs": [first.revision_ref],
    }
    assert [binding.revision_ref for binding in execution.skill_bindings] == [
        first.revision_ref
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("selected_keys", [[], ["skill-option:1"]])
async def test_later_context_phases_do_not_reselect_unbound_skill_candidates(
    tmp_path: Path,
    selected_keys: list[str],
) -> None:
    """Protocol characterization, not evidence of model relevance or quality."""
    library = SkillLibrary(tmp_path / "library")
    packages = [
        library.install(
            _write_skill(tmp_path / name, name=name, description=f"{name} procedure."),
            trust="trusted",
        )
        for name in ("Planning", "Delivery")
    ]
    agent = Agently.create_agent("skill-later-phase-boundary")
    agent.use_task_workspace(tmp_path / "work")
    agent.use_record_store(tmp_path / "records", mode="read_write")
    agent.skill_library = library
    request = _ExecutionAwareSelectionRequest(selected_keys)
    cast(Any, agent).create_temp_request = lambda: request
    execution = (
        agent.create_execution()
        .input("Plan and deliver a report")
        .use_skills([package.revision_ref for package in packages])
    )
    context = await execution.async_prepare_task_context()
    original_bindings = tuple(execution.skill_bindings)
    original_revision = context.snapshot().revision

    for phase in ("delivery_draft", "delivery_review"):
        package = await execution.async_read_task_context(
            consumer_id=f"worker:{phase}",
            phase=phase,
            intent=ContextReadIntent(
                query="Read the Delivery procedure for this later task phase",
                metadata={"exclude_already_in_prompt": True},
            ),
        )
        assert [block.content for block in package.blocks] == (
            ["Apply the Planning procedure."] if selected_keys else []
        )
        assert package.phase == phase
        assert package.task_context_id == context.context_id

    assert await execution.async_prepare_task_context() is context
    assert tuple(execution.skill_bindings) == original_bindings
    assert context.snapshot().revision == original_revision
    assert execution.diagnostics["skill_scope"]["model_decision_revision_refs"] == [
        package.revision_ref for package in packages
    ]
    assert request.call_count == 1


@pytest.mark.asyncio
async def test_required_skills_remain_readable_in_later_task_phases(tmp_path: Path) -> None:
    """Only availability/readback is asserted; no model consumption is claimed."""
    library = SkillLibrary(tmp_path / "library")
    packages = [
        library.install(
            _write_skill(tmp_path / name, name=name, description=f"{name} procedure."),
            trust="trusted",
        )
        for name in ("Planning", "Delivery")
    ]
    agent = Agently.create_agent("skill-required-later-phase")
    agent.use_task_workspace(tmp_path / "work")
    agent.use_record_store(tmp_path / "records", mode="read_write")
    agent.skill_library = library

    def unexpected_request() -> None:
        raise AssertionError("Required root readback must not need model selection")

    cast(Any, agent).create_temp_request = unexpected_request
    execution = agent.create_execution().require_skills(
        [package.revision_ref for package in packages]
    )
    await execution.async_prepare_task_context()
    for phase in ("planning", "delivery"):
        package = await execution.async_read_task_context(
            consumer_id=f"worker:{phase}", phase=phase,
        )
        assert {block.content for block in package.blocks} == {
            "Apply the Planning procedure.", "Apply the Delivery procedure.",
        }
    assert [binding.revision_ref for binding in execution.skill_bindings] == [
        package.revision_ref for package in packages
    ]
    assert execution.logs.get("context_consumptions", []) == []


@pytest.mark.asyncio
async def test_fresh_user_execution_reselects_agent_default_skills(tmp_path: Path) -> None:
    """A synthetic selector verifies execution isolation, not selection quality."""
    library = SkillLibrary(tmp_path / "library")
    packages = [
        library.install(
            _write_skill(tmp_path / name, name=name, description=f"{name} procedure."),
            trust="trusted",
        )
        for name in ("Planning", "Delivery")
    ]
    agent = Agently.create_agent("skill-new-user-execution")
    agent.use_task_workspace(tmp_path / "work")
    agent.use_record_store(tmp_path / "records", mode="read_write")
    agent.skill_library = library
    agent.use_skills([package.revision_ref for package in packages], always=True)
    request = _ExecutionAwareSelectionRequest(["skill-option:1"])
    cast(Any, agent).create_temp_request = lambda: request
    first = agent.create_execution().input("Plan the report")
    await first.async_prepare_task_context()

    request.selected_keys = ["skill-option:2"]
    later = agent.create_execution().input("Deliver the report")
    await later.async_prepare_task_context()

    assert first.id != later.id
    assert [binding.revision_ref for binding in first.skill_bindings] == [packages[0].revision_ref]
    assert [binding.revision_ref for binding in later.skill_bindings] == [packages[1].revision_ref]
    assert request.call_count == 2
    assert request.slots["input"]["input"] == "Deliver the report"
    assert request.observed_execution_context is later.execution_context


@pytest.mark.asyncio
async def test_execution_without_skill_declarations_does_not_read_global_library(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    library.install(
        _write_skill(tmp_path / "installed", name="Global Only", description="Not declared."),
        trust="trusted",
    )
    agent = Agently.create_agent("skill-no-global-scope-test").use_task_workspace(
        tmp_path / "work"
    )
    agent.skill_library = library
    execution = agent.create_execution().input("Answer without declared Skills")

    await execution.async_prepare_task_context()

    assert execution.skill_bindings == []
    assert execution.diagnostics["skill_scope"] == {
        "status": "frozen",
        "required_revision_refs": [],
        "model_decision_revision_refs": [],
    }


@pytest.mark.asyncio
async def test_execution_local_skill_reconfiguration_refreshes_scope_before_start(
    tmp_path: Path,
) -> None:
    library = SkillLibrary(tmp_path / "library")
    first = library.install(
        _write_skill(tmp_path / "first", name="First Required", description="First."),
        trust="trusted",
    )
    second = library.install(
        _write_skill(tmp_path / "second", name="Second Required", description="Second."),
        trust="trusted",
    )
    agent = Agently.create_agent("skill-local-refresh-test").use_task_workspace(
        tmp_path / "work"
    )
    agent.skill_library = library
    execution = (
        agent.create_execution()
        .input("Apply both procedures")
        .require_skills(first.skill_id)
    )

    await execution.async_prepare_task_context()
    execution.require_skills(second.skill_id)
    await execution.async_prepare_task_context()

    assert execution.diagnostics["skill_scope"] == {
        "status": "frozen",
        "required_revision_refs": [first.revision_ref, second.revision_ref],
        "model_decision_revision_refs": [],
    }
    assert [binding.revision_ref for binding in execution.skill_bindings] == [
        first.revision_ref,
        second.revision_ref,
    ]


@pytest.mark.asyncio
async def test_invalid_model_skill_key_fails_closed_without_binding(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(
        _write_skill(tmp_path / "skill", name="Safe Review", description="Review safely."),
        trust="trusted",
    )
    agent = Agently.create_agent("skill-selection-invalid-test").use_task_workspace(tmp_path / "work")
    agent.skill_library = library
    cast(Any, agent).create_temp_request = lambda: _SelectionRequest(["skill-option:unknown"])
    execution = agent.input("Review this").use_skills(package.skill_id)

    await execution.async_prepare_task_context()

    assert execution.skill_bindings == []
    assert execution.diagnostics["skill_selection"]["status"] == "invalid"


@pytest.mark.asyncio
async def test_required_skill_pack_resolves_to_exact_library_revisions(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    _write_skill(
        tmp_path / "pack" / "first",
        name="First Pack Skill",
        description="First procedure.",
    )
    _write_skill(
        tmp_path / "pack" / "second",
        name="Second Pack Skill",
        description="Second procedure.",
    )
    pack = library.install_pack(
        tmp_path / "pack",
        skill_pack_id="review-pack",
        name="Review Pack",
        trust="trusted",
    )
    agent = Agently.create_agent("skill-pack-binding-test").use_task_workspace(tmp_path / "work")
    agent.skill_library = library
    execution = agent.input("Run both procedures").use_skills_packs(
        pack.skill_pack_id,
        mode="required",
    )

    await execution.async_prepare_task_context()

    expected = {
        library.resolve("first-pack-skill").revision_ref,
        library.resolve("second-pack-skill").revision_ref,
    }
    assert set(pack.revision_refs) == expected
    assert {binding.revision_ref for binding in execution.skill_bindings} == expected
    assert all(binding.mode == "required" for binding in execution.skill_bindings)


@pytest.mark.asyncio
async def test_resolve_skills_plan_is_binding_and_route_preview(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "library")
    package = library.install(
        _write_skill(tmp_path / "skill", name="Plan Preview", description="Preview a plan."),
        trust="trusted",
    )
    agent = Agently.create_agent("skill-plan-preview-test").use_task_workspace(tmp_path / "work")
    agent.skill_library = library

    plan = await agent.async_resolve_skills_plan(
        "Preview this task",
        skills=[package.skill_id],
        mode="required",
    )

    assert plan["schema_version"] == "agently.skill_binding_plan.compat.v2"
    assert plan["status"] == "resolved"
    assert plan["selected_skills"][0]["revision_ref"] == package.revision_ref
    assert plan["route_preview"]["selected_route"] in {"model_request", "agent_task"}
    assert plan["route_preview"]["selected_route"] != "skills"


@pytest.mark.asyncio
async def test_required_skill_availability_joins_revision_selector_to_canonical_id(
    tmp_path: Path,
) -> None:
    from agently.builtins.plugins.AgentExecution.modules.task_strategy import (
        _resolve_required_skill_availability,
    )

    library = SkillLibrary(tmp_path / "library")
    package = library.install(
        _write_skill(
            tmp_path / "skill",
            name="Revision Identity",
            description="Exercise required Skill identity joins.",
        ),
        trust="trusted",
    )
    agent = Agently.create_agent("skill-required-revision-identity").use_task_workspace(
        tmp_path / "work"
    )
    agent.skill_library = library
    execution = agent.input("Use the exact installed revision").use_skills(
        package.revision_ref,
        mode="required",
    )
    await execution.async_prepare_task_context()

    assert execution.required_skill_ids() == [package.revision_ref]
    selected, failure = await _resolve_required_skill_availability(
        cast(Any, execution),
        goal="Use the exact installed revision",
    )

    assert selected == [package.skill_id]
    assert failure is None


class _OrdinaryExecution:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.status = "created"
        self.id = "ordinary-execution"

    def input(self, value: Any) -> "_OrdinaryExecution":
        self.calls.append(("input", value))
        return self

    def output(self, value: Any, **kwargs: Any) -> "_OrdinaryExecution":
        self.calls.append(("output", value))
        return self

    def use_skills(self, value: Any, **kwargs: Any) -> "_OrdinaryExecution":
        self.calls.append(("use_skills", {"value": value, **kwargs}))
        return self

    def strategy(self, value: str) -> "_OrdinaryExecution":
        self.calls.append(("strategy", value))
        return self

    async def async_get_data(self) -> dict[str, Any]:
        self.status = "success"
        self.calls.append(("async_get_data", None))
        return {"result": "done"}


@pytest.mark.asyncio
async def test_run_skills_task_adapts_an_ordinary_agent_execution(tmp_path: Path) -> None:
    agent = Agently.create_agent("skill-run-adapter-test").use_task_workspace(tmp_path)
    ordinary = _OrdinaryExecution()
    cast(Any, agent).create_execution = lambda **_: ordinary

    result = await agent.async_run_skills_task(
        "Run the task",
        skills=["installed-skill"],
        mode="required",
        output={"result": str},
    )

    assert [call[0] for call in ordinary.calls] == [
        "input",
        "output",
        "use_skills",
        "async_get_data",
    ]
    assert result.output == {"result": "done"}
    assert result.execution is ordinary


def test_agent_skills_extension_has_no_second_execution_owner() -> None:
    module = inspect.getmodule(SkillsExtension)
    assert module is not None
    source = inspect.getsource(module)

    for forbidden in (
        "SkillsManager",
        "async_execute_plan",
        "async_execute_skills_plan",
        "effort_strateg",
        "create_agent_skills_manager_context",
    ):
        assert forbidden not in source


def test_agent_task_planner_snapshot_contains_actions_not_skill_context(tmp_path: Path) -> None:
    from agently.builtins.plugins.AgentExecution.modules.task_strategy import (
        _planner_capability_snapshot,
    )

    class Execution:
        def action_candidates(self):
            return [{"action_id": "read_file", "description": "Read a file."}]

        def skill_candidate_summary(self):
            raise AssertionError("AgentTask planner must not read Skill candidates as capabilities.")

    assert _planner_capability_snapshot(cast(Any, Execution())) == [
        {
            "id": "read_file",
            "kind": "action",
            "route": "model_request",
            "guidance_access": "none",
            "description": "Read a file.",
        }
    ]


def test_agent_task_skill_lifecycle_event_names_do_not_claim_activation() -> None:
    from agently.builtins.plugins.AgentExecution.modules import (
        task_strategy,
    )

    source = inspect.getsource(task_strategy)

    assert '"skills.revisions.bound"' in source
    assert '"skills.activation.bound"' not in source
