"""Real ModelRequest transport, synthetic provider output; no quality claims."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from agently import Agently
from agently.core import PluginManager, SkillLibrary
from agently.types.data import AgentlyRequestData, ContextReadIntent
from agently.utils import DataFormatter, Settings


@pytest.mark.asyncio
async def test_bound_root_reaches_actual_selector_request_once(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    class CaptureRequester:
        name = "CaptureContextSelector"
        DEFAULT_SETTINGS: dict[str, Any] = {}

        def __init__(self, prompt: Any, settings: Any) -> None:
            self.prompt = prompt

        @staticmethod
        def _on_register() -> None:
            pass

        @staticmethod
        def _on_unregister() -> None:
            pass

        def generate_request_data(self) -> AgentlyRequestData:
            return AgentlyRequestData(
                client_options={},
                headers={},
                data={"messages": self.prompt.to_messages()},
                request_options={"stream": True},
                request_url="synthetic://context-selector",
            )

        async def request_model(self, request_data: AgentlyRequestData) -> AsyncGenerator[tuple[str, str], None]:
            calls.append(DataFormatter.sanitize(request_data.data))
            yield "message", '{"selected_keys": []}'

        async def broadcast_response(self, response_generator: Any) -> AsyncGenerator[tuple[str, str], None]:
            async for _, data in response_generator:
                yield "delta", data
                yield "done", data

    settings = Settings(name="context-transport", parent=Agently.settings)
    plugins = PluginManager(settings, parent=Agently.plugin_manager, name="context-transport")
    plugins.register("ModelRequester", CaptureRequester, activate=True)
    agent = Agently.AgentType(plugins, parent_settings=settings)
    agent.use_task_workspace(tmp_path / "work")
    agent.use_record_store(tmp_path / "records")
    agent.set_agent_prompt("instruct", "Keep the handoff suitable for external recipients.")
    skill_path = tmp_path / "skill"
    (skill_path / "references").mkdir(parents=True)
    root_text = "# Routing\nRead references/detail.md before handoff. ROOT_TRANSPORT_MARKER"
    (skill_path / "SKILL.md").write_text(
        "---\nname: transport-skill\ndescription: Handoff guidance.\n---\n\n" + root_text,
        encoding="utf-8",
    )
    (skill_path / "references/detail.md").write_text("OPTIONAL_UNREAD_BODY", encoding="utf-8")
    (skill_path / "references/other.md").write_text("OTHER_UNREAD_BODY", encoding="utf-8")
    agent.skill_library = SkillLibrary(tmp_path / "library")
    revision = agent.skill_library.install(skill_path, trust="trusted")
    execution = agent.create_execution().input("Prepare handoff").require_skills(revision.revision_ref)
    package = await execution.async_read_task_context(consumer_id="worker", phase="handoff")

    assert len(calls) == 1
    rendered = json.dumps(calls[0], ensure_ascii=False)
    assert rendered.count("ROOT_TRANSPORT_MARKER") == 1
    assert rendered.count("Keep the handoff suitable for external recipients.") == 1
    assert "selection_budget" not in rendered
    assert "estimated_chars" not in rendered
    assert "resource-index" not in rendered
    assert "OPTIONAL_UNREAD_BODY" not in rendered
    assert "OTHER_UNREAD_BODY" not in rendered
    assert revision.revision_ref not in rendered
    assert "#section-" not in rendered
    assert [block.content for block in package.blocks] == [root_text]

    root_ref = package.blocks[0].source_ref
    explicit = await execution.async_read_task_context(
        intent=ContextReadIntent(
            query="Read the exact reference",
            explicit_refs=(root_ref.rsplit("/", 1)[0] + "/references/detail.md",),
            metadata={"optional_selection": "none"},
        ),
        consumer_id="explicit",
        phase="handoff",
    )
    assert len(calls) == 1
    assert any(block.content == "OPTIONAL_UNREAD_BODY" for block in explicit.blocks)
