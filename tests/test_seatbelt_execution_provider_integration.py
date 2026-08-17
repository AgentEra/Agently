"""Observed macOS Seatbelt mechanism evidence."""

from __future__ import annotations

import json
import platform
from pathlib import Path

import pytest

from agently.builtins.plugins.CodeRuntimeAdapter import PythonCodeRuntimeAdapter
from agently.builtins.plugins.ExecutionResourceProvider.SeatbeltExecutionResourceProvider import (
    SeatbeltExecutionResourceProvider,
)
from agently.core.TaskWorkspace import TaskWorkspace
from agently.types.data import CodeExecutionRequest, TaskWorkspaceAccessRequirement


@pytest.mark.asyncio
async def test_seatbelt_enforces_granted_write_and_denies_ungranted_write(
    tmp_path: Path,
) -> None:
    if platform.system() != "Darwin":
        pytest.skip("real Seatbelt evidence requires macOS")

    workspace = TaskWorkspace(tmp_path / "workspace", execution_id="seatbelt-run")
    grant = workspace.issue_execution_access(
        action_call_id="run",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    ungranted = tmp_path / "outside.txt"
    source = (
        "import json\n"
        "from pathlib import Path\n"
        f"outside = Path({str(ungranted)!r})\n"
        "denied = False\n"
        "try:\n"
        "    outside.write_text('escape')\n"
        "except OSError:\n"
        "    denied = True\n"
        "Path('../output/result.json').write_text(json.dumps({'write_denied': denied}))\n"
    )
    bundle = PythonCodeRuntimeAdapter().prepare(
        CodeExecutionRequest.create(
            language="python",
            source_code=source,
            expected_outputs=["output/result.json"],
        ),
        policy={},
    )
    manifest = await workspace.materialize_execution_bundle(grant, bundle)
    provider = SeatbeltExecutionResourceProvider()
    handle = await provider.async_ensure(
        requirement={
            "kind": "code_execution",
            "required_capabilities": {"language": "python"},
            "task_workspace_access_grant": grant,
            "config": {"network": False},
        },
        policy={"timeout_seconds": 20, "max_output_bytes": 10000},
    )
    resource = handle["resource"]

    result = await resource.async_execute_code(
        bundle=bundle,
        manifest=manifest,
        grant=grant,
        timeout=20,
    )

    assert handle["meta"]["mechanism_verified"] is True
    assert result["ok"] is True, result
    observed = json.loads((Path(grant.execution_area) / "output" / "result.json").read_text())
    assert observed == {"write_denied": True}
    assert not ungranted.exists()
