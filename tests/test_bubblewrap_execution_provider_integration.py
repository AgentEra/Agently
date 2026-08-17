"""Observed Linux Bubblewrap namespace and filesystem evidence."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

import pytest

from agently.builtins.plugins.CodeRuntimeAdapter import PythonCodeRuntimeAdapter
from agently.builtins.plugins.ExecutionResourceProvider.BubblewrapExecutionResourceProvider import (
    BubblewrapExecutionResourceProvider,
)
from agently.core.TaskWorkspace import TaskWorkspace
from agently.types.data import CodeExecutionRequest, TaskWorkspaceAccessRequirement


@pytest.mark.asyncio
async def test_bubblewrap_enforces_grant_mounts_and_network_namespace(tmp_path: Path) -> None:
    if platform.system() != "Linux":
        pytest.skip("real Bubblewrap evidence requires Linux")

    workspace = TaskWorkspace(tmp_path / "workspace", execution_id="bubblewrap-run")
    grant = workspace.issue_execution_access(
        action_call_id="run",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    host_net_ns = os.stat("/proc/self/ns/net").st_ino
    source = (
        "import json, os\n"
        "from pathlib import Path\n"
        "shadow_denied = False\n"
        "try:\n"
        "    Path('/etc/shadow').read_text()\n"
        "except OSError:\n"
        "    shadow_denied = True\n"
        "Path('../output/result.json').write_text(json.dumps({\n"
        "    'shadow_denied': shadow_denied,\n"
        "    'net_ns': os.stat('/proc/self/ns/net').st_ino,\n"
        "}))\n"
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
    provider = BubblewrapExecutionResourceProvider()
    handle = await provider.async_ensure(
        requirement={
            "kind": "code_execution",
            "required_capabilities": {"language": "python"},
            "task_workspace_access_grant": grant,
            "config": {"network": False},
        },
        policy={"timeout_seconds": 20, "max_output_bytes": 10000},
    )
    result = await handle["resource"].async_execute_code(
        bundle=bundle,
        manifest=manifest,
        grant=grant,
        timeout=20,
    )

    assert handle["meta"]["mechanism_verified"] is True
    assert result["ok"] is True, result
    observed = json.loads((Path(grant.execution_area) / "output" / "result.json").read_text())
    assert observed["shadow_denied"] is True
    assert observed["net_ns"] != host_net_ns
