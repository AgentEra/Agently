"""Select optional Skills using the complete declared task (4.1.4.9).

Set AGENTLY_BASE_URL, AGENTLY_API_KEY and AGENTLY_MODEL.
Optional AGENTLY_REQUEST_OPTIONS supplies a JSON object of provider options.

Working principle:
    all goals + criteria + original task facts -> one real selection request
    -> validated host keys -> exact Skill bindings

This example inspects preparation only. It does not execute the probe or
claim that binding a Skill authorizes its script.

Expected key output from a real local Qwen run:
    selected=["Release Checklist", "script-release-probe"]
    unchanged_preparation_reuses_bindings=True
The observed run made one selection request; repeated preparation made none.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agently import Agently  # noqa: E402
from agently.core import SkillLibrary  # noqa: E402


async def run_example() -> dict[str, object]:
    with TemporaryDirectory(prefix="agently-complete-task-") as directory:
        root = Path(directory)
        agent = Agently.create_agent().use_task_workspace(root / "files")
        agent.set_settings("plugins.ModelRequester.OpenAICompatible", {
            "base_url": os.environ["AGENTLY_BASE_URL"],
            "auth": os.environ["AGENTLY_API_KEY"],
            "model": os.environ["AGENTLY_MODEL"],
            "stream": False,
            "request_options": json.loads(os.getenv("AGENTLY_REQUEST_OPTIONS", "{}")),
        })
        agent.skill_library = SkillLibrary(root / "library")
        packages = [
            agent.skill_library.install(Path(__file__).parent / "skills" / name, trust="trusted")
            for name in ("release-checklist", "script-release-probe")
        ]
        execution = (
            agent.create_execution()
            .goal(
                ["Prepare the release-readiness checklist", "Run the component probe and report its observed status and token"],
                success_criteria=["Base the report on actual release facts and probe output"],
                turn_on_long_task=False,
            )
            .input({"release": "4.1.4.9", "component": "Shell Runtime"})
            .use_skills([package.revision_ref for package in packages])
        )
        await execution.async_prepare_task_context()
        first_bindings = tuple(execution.skill_bindings)
        await execution.async_prepare_task_context()
        selected = {binding.revision_ref for binding in execution.skill_bindings}
        return {
            "selected": [package.name for package in packages if package.revision_ref in selected],
            "unchanged_preparation_reuses_bindings": tuple(execution.skill_bindings) == first_bindings,
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_example()), ensure_ascii=False, indent=2))
