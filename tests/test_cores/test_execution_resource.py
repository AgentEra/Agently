import pytest
from typing import Any, cast
import importlib
import sys
import types
import uuid
from pathlib import Path

from agently import Agently
from agently.builtins.plugins.CodeRuntimeAdapter import get_code_runtime_adapter
from agently.builtins.plugins.ExecutionResourceProvider._bounded_process import (
    BoundedProcessResult,
)
from agently.core import (
    ExecutionResourceApprovalDenied,
    ExecutionResourceError,
    ExecutionResourceManager,
)
from agently.core.TaskWorkspace import TaskWorkspace
from agently.types.data import (
    CodeExecutionRequest,
    ExecutionResourceHandle,
    ExecutionResourceRequirement,
    TaskWorkspaceAccessRequirement,
)
from agently.utils import Settings


def _create_manager():
    settings = Settings(name="ExecutionResourceTestSettings", parent=Agently.settings)
    return ExecutionResourceManager(
        plugin_manager=Agently.plugin_manager,
        settings=settings,
        event_center=Agently.event_center,
    )


async def _materialized_code_bundle(
    tmp_path: Path,
    *,
    language: str,
    source_code: str,
    args: tuple[str, ...] = (),
):
    workspace = TaskWorkspace(
        tmp_path / f"{language}-workspace",
        execution_id=f"{language}-execution",
    )
    grant = workspace.issue_execution_access(
        action_call_id=f"run-{language}",
        requirement=TaskWorkspaceAccessRequirement(mode="snapshot"),
    )
    request = CodeExecutionRequest.create(
        language=language,
        source_code=source_code,
        args=args,
    )
    bundle = get_code_runtime_adapter(language).prepare(
        request,
        policy={"dependency_install": "deny"},
    )
    manifest = await workspace.materialize_execution_bundle(grant, bundle)
    return bundle, manifest, grant


def test_execution_resource_declare_is_lazy():
    manager = _create_manager()
    requirement = manager.declare(
        {
            "kind": "test_lazy_resource",
            "scope": "action_call",
            "resource_key": "lazy_test",
        }
    )

    assert requirement["kind"] == "test_lazy_resource"
    assert manager.list() == []


@pytest.mark.asyncio
async def test_execution_resource_ensure_reuses_and_releases_handle():
    manager = _create_manager()
    requirement = cast(ExecutionResourceRequirement, {
        "kind": "bash",
        "scope": "session",
        "owner_id": "session-1",
        "resource_key": "bash_test",
    })

    handle_1 = await manager.async_ensure(requirement)
    handle_2 = await manager.async_ensure(requirement)

    assert handle_1.get("handle_id") == handle_2.get("handle_id")
    assert handle_2.get("ref_count") == 2

    await manager.async_release(handle_1)
    assert manager.list()[0].get("ref_count") == 1
    await manager.async_release(handle_2)
    assert manager.list() == []


@pytest.mark.asyncio
async def test_execution_resource_release_failure_is_structured_and_quarantined() -> None:
    manager = _create_manager()

    class FailingReleaseProvider:
        name = "FailingReleaseProvider"
        provider_id = "failing-release"
        supported_kinds = ("failing_release",)
        DEFAULT_SETTINGS: dict[str, Any] = {}

        async def async_probe(self, *, requirement: Any, policy: Any) -> Any:
            _ = requirement, policy
            return {
                "provider_id": self.provider_id,
                "available": True,
                "supported_kinds": list(self.supported_kinds),
                "capabilities": {},
                "reason": "fixture",
            }

        async def async_ensure(
            self, *, requirement: Any, policy: Any, existing_handle: Any = None
        ) -> Any:
            _ = requirement, policy, existing_handle
            return {
                "handle_id": "failing-release:1",
                "resource": object(),
                "status": "ready",
            }

        async def async_health_check(self, handle: Any) -> Any:
            _ = handle
            return "ready"

        async def async_release(self, handle: Any) -> None:
            _ = handle
            raise RuntimeError("resource is still live")

    manager.register_provider(cast(Any, FailingReleaseProvider()))
    handle = await manager.async_ensure(
        {
            "kind": "failing_release",
            "provider_id": "failing-release",
        }
    )

    with pytest.raises(ExecutionResourceError) as raised:
        await manager.async_release(handle)

    assert raised.value.code == "execution_resource.release_failed"
    inspected = cast(ExecutionResourceHandle, manager.inspect("failing-release:1"))
    assert inspected.get("status") == "failed"
    assert inspected.get("meta", {}).get("cleanup_error") == (
        "resource is still live"
    )


@pytest.mark.asyncio
async def test_execution_resource_rechecks_health_before_reuse():
    manager = _create_manager()

    class FlakyProvider:
        name = "FlakyProvider"
        kind = "flaky"
        DEFAULT_SETTINGS: dict[str, Any] = {}

        def __init__(self):
            self.ensure_count = 0
            self.release_count = 0

        async def async_ensure(self, *, requirement, policy, existing_handle=None):
            _ = (requirement, policy, existing_handle)
            self.ensure_count += 1
            return {
                "handle_id": f"flaky:{ self.ensure_count }",
                "resource": object(),
                "status": "ready",
                "meta": {"provider": self.name},
            }

        async def async_health_check(self, handle):
            _ = handle
            return "unhealthy"

        async def async_release(self, handle):
            _ = handle
            self.release_count += 1

    provider = FlakyProvider()
    manager.register_provider(cast(Any, provider))
    requirement = cast(ExecutionResourceRequirement, {
        "kind": "flaky",
        "scope": "session",
        "owner_id": "session-health",
        "resource_key": "resource",
    })

    handle_1 = await manager.async_ensure(requirement)
    handle_2 = await manager.async_ensure(requirement)

    assert handle_1.get("handle_id") == "flaky:1"
    assert handle_2.get("handle_id") == "flaky:2"
    assert provider.ensure_count == 2
    assert provider.release_count == 1
    assert manager.list()[0].get("handle_id") == "flaky:2"

    await manager.async_release(handle_2)
    assert manager.list() == []


@pytest.mark.asyncio
async def test_execution_resource_default_policy_denies_and_does_not_start():
    manager = _create_manager()

    with pytest.raises(ExecutionResourceApprovalDenied):
        await manager.async_ensure(
            {
                "kind": "test_approval_resource",
                "scope": "action_call",
                "resource_key": "approval_test",
                "approval_required": True,
            }
        )

    assert manager.list() == []


def test_action_python_sandbox_uses_execution_resource():
    action_id = "python_env_action"
    Agently.action.register_python_sandbox_action(action_id=action_id, expose_to_model=False)

    result = Agently.action.execute_action(action_id, {"source_code": "print(40 + 2)"})

    assert result.get("status") == "success"
    result_data = cast(dict[str, Any], result.get("data"))
    assert result_data["stdout"] == "42\n"
    assert Agently.execution_resource.list(scope="action_call") == []


def test_default_python_docker_sandbox_fails_closed_when_docker_unavailable(monkeypatch):
    docker_module = importlib.import_module(
        "agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider"
    )

    monkeypatch.setattr(docker_module.shutil, "which", lambda binary: None)
    action_id = f"docker_missing_python_{uuid.uuid4().hex[:8]}"
    agent = Agently.create_agent()
    agent.enable_python(action_id=action_id)

    result = agent.action.execute_action(action_id, {"source_code": "print(1)"})

    assert result.get("status") == "error"
    assert result.get("success") is False
    diagnostics = result.get("diagnostics", [])
    assert any(item.get("code") == "execution_resource.provider_unavailable" for item in diagnostics)
    assert Agently.execution_resource.list(scope="action_call") == []


@pytest.mark.asyncio
async def test_docker_runtime_strict_profile_reports_missing_image_without_pull(monkeypatch):
    docker_module = importlib.import_module(
        "agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider"
    )
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        _ = kwargs
        calls.append([str(item) for item in args])
        if args[:2] == ["docker", "version"]:
            return types.SimpleNamespace(returncode=0, stdout="26.0.0\n", stderr="")
        if args[:3] == ["docker", "image", "inspect"]:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="No such image")
        raise AssertionError(f"unexpected docker command: {args}")

    monkeypatch.setattr(docker_module.shutil, "which", lambda binary: f"/usr/bin/{ binary }")
    monkeypatch.setattr(docker_module.subprocess, "run", fake_run)

    manager = _create_manager()
    with pytest.raises(ExecutionResourceError) as exc_info:
        await manager.async_ensure(
            cast(ExecutionResourceRequirement, {
                "kind": "docker",
                "scope": "action_call",
                "resource_key": "strict_missing_node",
                "config": {
                    "runtime_profile": {
                        "language": "nodejs",
                        "image": "node:22-slim",
                        "provisioning_profile": "strict",
                        "image_pull_policy": "never",
                    }
                },
                "policy": {"timeout_seconds": 20},
            })
        )

    assert exc_info.value.code == "execution_resource.docker_image_missing"
    assert any(call[:3] == ["docker", "image", "inspect"] for call in calls)
    assert not any(call[:2] == ["docker", "pull"] for call in calls)
    assert manager.list() == []


@pytest.mark.asyncio
async def test_docker_runtime_developer_profile_pulls_missing_image(monkeypatch):
    docker_module = importlib.import_module(
        "agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider"
    )
    calls: list[list[str]] = []
    pulled = False

    def fake_run(args, **kwargs):
        nonlocal pulled
        _ = kwargs
        calls.append([str(item) for item in args])
        if args[:2] == ["docker", "version"]:
            return types.SimpleNamespace(returncode=0, stdout="26.0.0\n", stderr="")
        if args[:3] == ["docker", "image", "inspect"]:
            return types.SimpleNamespace(returncode=0 if pulled else 1, stdout="sha256:test\n" if pulled else "", stderr="")
        if args[:2] == ["docker", "pull"]:
            pulled = True
            return types.SimpleNamespace(returncode=0, stdout="pulled node:22-slim\n", stderr="")
        raise AssertionError(f"unexpected docker command: {args}")

    monkeypatch.setattr(docker_module.shutil, "which", lambda binary: f"/usr/bin/{ binary }")
    monkeypatch.setattr(docker_module.subprocess, "run", fake_run)

    async def fake_async_pull(self, image, *, timeout=None):
        _ = self, timeout
        nonlocal pulled
        pulled = True
        calls.append(["docker", "pull", image])
        return {"ok": True, "image": image, "returncode": 0, "stdout": f"pulled {image}\n", "stderr": ""}

    monkeypatch.setattr(
        docker_module.DockerExecutionResource,
        "async_pull_image",
        fake_async_pull,
    )

    manager = _create_manager()
    handle = await manager.async_ensure(
        cast(ExecutionResourceRequirement, {
            "kind": "docker",
            "scope": "action_call",
            "resource_key": "developer_node",
            "config": {
                "runtime_profile": {
                    "language": "nodejs",
                    "image": "node:22-slim",
                    "provisioning_profile": "developer",
                }
            },
            "policy": {"timeout_seconds": 20},
        })
    )

    meta = cast(dict[str, Any], handle.get("meta", {}))
    profile = cast(dict[str, Any], meta.get("runtime_profile", {}))
    image_preparation = cast(dict[str, Any], meta.get("image_preparation", {}))
    assert profile["image_pull_policy"] == "if_missing"
    assert profile["dependency_policy"] == {"mode": "install"}
    assert image_preparation["status"] == "pulled"
    assert any(call[:2] == ["docker", "pull"] and call[-1] == "node:22-slim" for call in calls)
    await manager.async_release(handle)


@pytest.mark.asyncio
async def test_docker_cpp_runtime_executes_adapter_bundle_without_provider_script(
    monkeypatch,
    tmp_path,
):
    docker_module = importlib.import_module(
        "agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider"
    )
    docker_calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        _ = kwargs
        if args[:3] == ["docker", "image", "inspect"]:
            return types.SimpleNamespace(returncode=0, stdout="sha256:gcc\n", stderr="")
        raise AssertionError(f"unexpected docker command: {args}")

    async def fake_bounded_process(args, **kwargs):
        _ = kwargs
        docker_calls.append([str(item) for item in args])
        return BoundedProcessResult(
            returncode=0,
            stdout=b"hello\n",
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
        )

    monkeypatch.setattr(docker_module.shutil, "which", lambda binary: f"/usr/bin/{ binary }")
    monkeypatch.setattr(docker_module.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_module, "run_bounded_process", fake_bounded_process)

    bundle, manifest, grant = await _materialized_code_bundle(
        tmp_path,
        language="cpp",
        source_code='#include <iostream>\nint main(){ std::cout << "hello\\n"; }\n',
        args=("arg1",),
    )
    resource = docker_module.DockerExecutionResource(
        runtime_profile={
            "provisioning_profile": "strict",
            "image_pull_policy": "never",
        },
        workspace_grant=grant,
    )
    result = await resource.async_execute_code(
        bundle=bundle,
        manifest=manifest,
        grant=grant,
        timeout=10,
    )

    assert result["ok"] is True
    assert len(docker_calls) == 2
    build_call, run_call = docker_calls
    build_image_index = build_call.index("gcc:14")
    run_image_index = run_call.index("gcc:14")
    assert build_call[build_image_index + 1 :] == [
        "c++", "-std=c++20", "-o", "../build/app", "main.cpp"
    ]
    assert run_call[run_image_index + 1 :] == ["../build/app", "arg1"]
    assert any(item.endswith(":/workspace/source:ro") for item in build_call)
    assert (Path(grant.execution_area) / "source" / "main.cpp").is_file()


@pytest.mark.asyncio
async def test_docker_go_runtime_executes_adapter_bundle_with_workspace_env(
    monkeypatch,
    tmp_path,
):
    docker_module = importlib.import_module(
        "agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider"
    )
    docker_calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        _ = kwargs
        if args[:3] == ["docker", "image", "inspect"]:
            return types.SimpleNamespace(returncode=0, stdout="sha256:go\n", stderr="")
        raise AssertionError(f"unexpected docker command: {args}")

    async def fake_bounded_process(args, **kwargs):
        _ = kwargs
        docker_calls.append([str(item) for item in args])
        return BoundedProcessResult(
            returncode=0,
            stdout=b"hello\n",
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
        )

    monkeypatch.setattr(docker_module.shutil, "which", lambda binary: f"/usr/bin/{ binary }")
    monkeypatch.setattr(docker_module.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_module, "run_bounded_process", fake_bounded_process)

    bundle, manifest, grant = await _materialized_code_bundle(
        tmp_path,
        language="go",
        source_code='package main\nimport "fmt"\nfunc main(){ fmt.Println("hello") }\n',
    )
    resource = docker_module.DockerExecutionResource(
        runtime_profile={
            "provisioning_profile": "strict",
            "image_pull_policy": "never",
        },
        workspace_grant=grant,
    )
    result = await resource.async_execute_code(
        bundle=bundle,
        manifest=manifest,
        grant=grant,
        timeout=10,
    )

    assert result["ok"] is True
    assert len(docker_calls) == 2
    build_call, run_call = docker_calls
    build_image_index = build_call.index("golang:1")
    run_image_index = run_call.index("golang:1")
    assert build_call[build_image_index + 1 :] == [
        "go", "build", "-o", "../build/app", "main.go"
    ]
    assert "GOCACHE=/workspace/build/go-build-cache" in build_call
    assert "GOMODCACHE=/workspace/build/go-mod-cache" in build_call
    assert run_call[run_image_index + 1 :] == ["../build/app"]
    assert (Path(grant.execution_area) / "source" / "main.go").is_file()


def test_action_bash_sandbox_uses_execution_resource(tmp_path):
    action_id = "bash_env_action"
    Agently.action.register_bash_sandbox_action(
        action_id=action_id,
        expose_to_model=False,
        allowed_cmd_prefixes=["pwd"],
        allowed_workdir_roots=[str(tmp_path)],
    )

    result = Agently.action.execute_action(action_id, {"cmd": "pwd", "workdir": str(tmp_path)})

    assert result.get("status") == "success"
    result_data = cast(dict[str, Any], result.get("data"))
    assert result_data["ok"] is True
    assert str(tmp_path) in result_data["stdout"]
    assert Agently.execution_resource.list(scope="action_call") == []


def test_bash_execution_resource_materializes_task_workspace_boundary(tmp_path):
    # Provider-side file-boundary materialization: a TaskWorkspace-issued root that
    # does not yet exist is created by the provider before the executor runs
    # (spec section 8.6).
    boundary = tmp_path / "lineage" / "executions" / "exec-mat" / "files"
    assert not boundary.exists()
    action_id = "bash_boundary_materialize"
    Agently.action.register_bash_sandbox_action(
        action_id=action_id,
        expose_to_model=False,
        allowed_cmd_prefixes=["pwd"],
        allowed_workdir_roots=[str(boundary)],
    )

    result = Agently.action.execute_action(action_id, {"cmd": "pwd"})

    assert result.get("status") == "success"
    assert boundary.is_dir()
    result_data = cast(dict[str, Any], result.get("data"))
    assert result_data["ok"] is True
    assert str(boundary.resolve()) in result_data["stdout"]


def test_action_environment_default_policy_denies_as_blocked_action_result():
    action_id = "approval_env_action"
    Agently.action.register_python_sandbox_action(action_id=action_id, expose_to_model=False)
    spec = Agently.action.action_registry.get_spec(action_id)
    assert spec is not None
    spec.get("execution_resources", [])[0]["approval_required"] = True

    result = Agently.action.execute_action(action_id, {"source_code": "print(1)"})

    assert result.get("status") == "blocked"
    assert "non-interactive environment" in str(result.get("error", ""))


@pytest.mark.asyncio
async def test_custom_action_executor_signature_still_works():
    action_id = "custom_executor_env_compat"

    class EchoExecutor:
        kind = "echo"
        sandboxed = False

        async def execute(self, *, spec, action_call, policy, settings):
            return {
                "spec": spec["action_id"],
                "input": action_call["action_input"],
                "policy": policy,
                "settings": settings.name,
            }

    Agently.action.register_action(
        action_id=action_id,
        desc="Compatibility executor.",
        kwargs={"value": (int, "")},
        executor=EchoExecutor(),
        expose_to_model=False,
    )

    result = await Agently.action.async_execute_action(action_id, {"value": 7})

    assert result.get("status") == "success"
    result_data = cast(dict[str, Any], result.get("data"))
    assert result_data["spec"] == action_id
    assert result_data["input"] == {"value": 7}


@pytest.mark.asyncio
async def test_execution_resource_release_scope_cleans_handles():
    manager = _create_manager()
    owner = "scope-test-owner"

    agent_requirement = cast(
        ExecutionResourceRequirement,
        {
            "kind": "bash",
            "scope": "agent",
            "owner_id": owner,
            "resource_key": "bash1",
        },
    )
    await manager.async_ensure(agent_requirement)
    reused_agent_handle = await manager.async_ensure(agent_requirement)
    await manager.async_ensure(
        {"kind": "bash", "scope": "session", "owner_id": owner, "resource_key": "bash2"},
    )
    await manager.async_ensure(
        {"kind": "bash", "scope": "agent", "owner_id": "other-owner", "resource_key": "bash3"},
    )

    assert len(manager.list(scope="agent", owner_id=owner)) == 1
    assert reused_agent_handle.get("ref_count") == 2
    assert len(manager.list(scope="session", owner_id=owner)) == 1

    await manager.async_release_scope("agent", owner)

    assert manager.list(scope="agent", owner_id=owner) == []
    assert len(manager.list(scope="session", owner_id=owner)) == 1
    assert len(manager.list(scope="agent", owner_id="other-owner")) == 1

    await manager.async_release_scope("session", owner)
    assert manager.list(scope="session", owner_id=owner) == []


@pytest.mark.asyncio
async def test_execution_resource_missing_provider_raises_stable_error():
    manager = _create_manager()

    with pytest.raises(ExecutionResourceError) as exc_info:
        await manager.async_ensure(
            {"kind": "nonexistent_provider_xyz", "scope": "action_call", "resource_key": "nope"},
        )

    error = exc_info.value
    assert hasattr(error, "code")
    assert error.code == "execution_resource.provider_missing"
    assert manager.list() == []


@pytest.mark.asyncio
async def test_execution_resource_approval_denied_returns_blocked_action_result():
    action_id = "denied_approval_env_action"
    Agently.action.register_python_sandbox_action(action_id=action_id, expose_to_model=False)
    spec = Agently.action.action_registry.get_spec(action_id)
    assert spec is not None
    spec.get("execution_resources", [])[0]["approval_required"] = True

    Agently.policy_approval.register_handler(
        "deny_execution_resource_test",
        lambda request: {"status": "denied", "reason": "Denied by test policy."},
        replace=True,
    )
    Agently.configure_policy_approval(handler="deny_execution_resource_test")
    try:
        result = Agently.action.execute_action(action_id, {"source_code": "print(1)"})
    finally:
        Agently.configure_policy_approval(handler="input_timeout_fail")
        Agently.policy_approval.unregister_handler("deny_execution_resource_test")

    assert result.get("status") == "blocked"
    assert Agently.execution_resource.list(scope="action_call") == []


@pytest.mark.asyncio
async def test_execution_resource_provider_failure_does_not_poison_registry():
    from agently.core.operation.ExecutionResource import ExecutionResourceManager
    from agently.utils import Settings

    settings = Settings(name="FailProviderTestSettings", parent=Agently.settings)
    manager = ExecutionResourceManager(
        plugin_manager=Agently.plugin_manager,
        settings=settings,
        event_center=Agently.event_center,
    )

    class FailingProvider:
        name = "FailingProvider"
        provider_id = "FailingProvider"
        kind = "test_failing_resource"
        supported_kinds = ("test_failing_resource",)
        DEFAULT_SETTINGS: dict[str, Any] = {}

        @staticmethod
        def _on_register():
            pass

        @staticmethod
        def _on_unregister():
            pass

        async def async_probe(self, *, requirement: Any, policy: Any) -> Any:
            _ = requirement, policy
            return {
                "provider_id": self.provider_id,
                "available": True,
                "supported_kinds": list(self.supported_kinds),
            }

        async def async_ensure(
            self, *, requirement: Any, policy: Any, existing_handle: Any = None
        ) -> Any:
            raise RuntimeError("Simulated provider failure")

        async def async_health_check(self, handle: Any) -> Any:
            return "unhealthy"

        async def async_release(self, handle: Any) -> None:
            pass

    manager.register_provider(FailingProvider())

    declared = manager.declare(
        {"kind": "test_failing_resource", "scope": "action_call", "resource_key": "fail_test"}
    )
    requirement_id = declared.get("requirement_id")
    assert requirement_id is not None

    with pytest.raises(RuntimeError, match="Simulated provider failure"):
        await manager.async_ensure(
            {
                "kind": "test_failing_resource",
                "scope": "action_call",
                "resource_key": "fail_test",
            }
        )

    assert manager.list() == []
    assert manager.inspect(requirement_id) is not None


@pytest.mark.asyncio
async def test_execution_resource_emits_probe_progress_and_actionable_failure():
    class RecordingEventCenter:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def emit(self, event: dict[str, Any]) -> None:
            self.events.append(event)

        async def async_emit(self, event: dict[str, Any]) -> None:
            self.events.append(event)

    event_center = RecordingEventCenter()
    manager = ExecutionResourceManager(
        plugin_manager=Agently.plugin_manager,
        settings=Settings(name="ExecutionResourceEventTest", parent=Agently.settings),
        event_center=cast(Any, event_center),
    )

    class UnavailableDockerProvider:
        provider_id = "unavailable-docker"
        supported_kinds = ("code_execution",)

        async def async_probe(self, *, requirement: Any, policy: Any) -> Any:
            _ = requirement, policy
            return {
                "provider_id": self.provider_id,
                "available": False,
                "supported_kinds": ["code_execution"],
                "capabilities": {},
                "reason": "Docker daemon is unavailable",
                "meta": {"availability": {"available": False}},
            }

    manager.register_provider(cast(Any, UnavailableDockerProvider()))
    with pytest.raises(ExecutionResourceError) as raised:
        await manager.async_ensure(
            {
                "kind": "code_execution",
                "provider_id": "unavailable-docker",
                "required_capabilities": {},
            }
        )

    assert raised.value.code == "execution_resource.provider_unavailable"
    event_types = [event["event_type"] for event in event_center.events]
    assert event_types == ["execution_resource.ensuring", "execution_resource.failed"]
    failure = event_center.events[-1]["payload"]
    assert failure["provider_id"] == "unavailable-docker"
    assert failure["reason"] == "Docker daemon is unavailable"
    assert "trusted_local" in failure["suggestion"]


@pytest.mark.asyncio
async def test_execution_resource_forwards_provider_preparation_progress():
    class RecordingEventCenter:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def emit(self, event: dict[str, Any]) -> None:
            self.events.append(event)

        async def async_emit(self, event: dict[str, Any]) -> None:
            self.events.append(event)

    class ProgressProvider:
        provider_id = "progress"
        supported_kinds = ("progress_resource",)

        def _bind_runtime_event_emitter(self, emitter):
            self.emitter = emitter

        async def async_probe(self, *, requirement: Any, policy: Any) -> Any:
            _ = requirement, policy
            return {
                "provider_id": self.provider_id,
                "available": True,
                "supported_kinds": list(self.supported_kinds),
                "capabilities": {},
                "reason": "ready",
            }

        async def async_ensure(self, *, requirement: Any, policy: Any, existing_handle: Any = None) -> Any:
            _ = policy, existing_handle
            await self.emitter(
                requirement,
                phase="image_pull_progress",
                message="Downloading layer 1/2",
                details={"image": "example:test"},
            )
            return {"handle_id": "progress:1", "resource": object(), "status": "ready"}

        async def async_health_check(self, handle: Any) -> Any:
            _ = handle
            return "ready"

        async def async_release(self, handle: Any) -> None:
            _ = handle

    event_center = RecordingEventCenter()
    manager = ExecutionResourceManager(
        plugin_manager=Agently.plugin_manager,
        settings=Settings(name="ExecutionResourceProgressTest", parent=Agently.settings),
        event_center=cast(Any, event_center),
    )
    manager.register_provider(cast(Any, ProgressProvider()))
    await manager.async_ensure({"kind": "progress_resource", "provider_id": "progress"})

    event_types = [event["event_type"] for event in event_center.events]
    assert event_types == [
        "execution_resource.ensuring",
        "execution_resource.probed",
        "execution_resource.progress",
        "execution_resource.ready",
    ]
    assert event_center.events[2]["payload"]["phase"] == "image_pull_progress"
    assert event_center.events[2]["payload"]["image"] == "example:test"


@pytest.mark.asyncio
async def test_mcp_executor_transport_routing():
    import unittest.mock as mock
    from agently.builtins.plugins.ActionExecutor.MCPActionExecutor import MCPActionExecutor

    direct_transport = object()
    executor = MCPActionExecutor(
        action_id="my_tool",
        transport=direct_transport,
        resource_key="shared_mcp_server",
    )

    captured: list[Any] = []

    def fake_client(transport):
        captured.append(transport)
        ctx = mock.MagicMock()
        ctx.__aenter__ = mock.AsyncMock(side_effect=Exception("stop"))
        ctx.__aexit__ = mock.AsyncMock(return_value=False)
        return ctx

    fake_fastmcp = types.ModuleType("fastmcp")
    fake_fastmcp.Client = fake_client  # type: ignore[attr-defined]
    fake_mcp = types.ModuleType("mcp")
    fake_mcp_types = types.ModuleType("mcp.types")
    for name in ("AudioContent", "EmbeddedResource", "ImageContent", "ResourceLink", "TextContent"):
        setattr(fake_mcp_types, name, type(name, (), {}))

    spec = {"action_id": "my_tool"}
    policy: dict[str, Any] = {}
    settings = mock.MagicMock()
    mcp_executor_module = importlib.import_module(
        "agently.builtins.plugins.ActionExecutor.MCPActionExecutor"
    )

    with (
        mock.patch.dict(sys.modules, {"fastmcp": fake_fastmcp, "mcp": fake_mcp, "mcp.types": fake_mcp_types}),
        mock.patch.object(mcp_executor_module.LazyImport, "import_package") as lazy_import,
        mock.patch("fastmcp.Client", fake_client),
    ):
        action_call_no_env: dict[str, Any] = {
            "action_input": {},
            "execution_resource_resources": {},
        }
        try:
            await executor.execute(spec=spec, action_call=action_call_no_env, policy=policy, settings=settings)
        except Exception:
            pass
        assert captured and captured[-1] is direct_transport, \
            "Without managed resource, executor must use direct transport"

        captured.clear()

        managed_client = mock.MagicMock()
        managed_client.call_tool = mock.AsyncMock(
            return_value=types.SimpleNamespace(
                is_error=False,
                structured_content={"session": "shared"},
                content=[],
            )
        )
        action_call_with_env: dict[str, Any] = {
            "action_input": {},
            "execution_resource_resources": {"shared_mcp_server": managed_client},
        }
        result = await executor.execute(
            spec=spec,
            action_call=action_call_with_env,
            policy=policy,
            settings=settings,
        )
        assert captured == [], "A managed MCP client must be reused without opening another session"
        managed_client.call_tool.assert_awaited_once_with(
            name="my_tool",
            arguments={},
            raise_on_error=False,
        )
        assert result == {"session": "shared"}
        assert lazy_import.call_args_list == [
            mock.call("fastmcp", version_constraint=">=3", auto_install=False),
            mock.call("mcp", auto_install=False),
            mock.call("fastmcp", version_constraint=">=3", auto_install=False),
            mock.call("mcp", auto_install=False),
        ]


@pytest.mark.asyncio
async def test_mcp_execution_resource_provider_owns_client_lifecycle():
    import unittest.mock as mock
    from agently.builtins.plugins.ExecutionResourceProvider.MCPExecutionResourceProvider import (
        MCPExecutionResourceProvider,
    )

    class FakeClient:
        def __init__(self, transport):
            self.transport = transport
            self.connected = False
            self.closed = False

        async def __aenter__(self):
            self.connected = True
            return self

        def is_connected(self):
            return self.connected and not self.closed

        async def close(self):
            self.closed = True

    fake_fastmcp = types.ModuleType("fastmcp")
    fake_fastmcp.Client = FakeClient  # type: ignore[attr-defined]
    provider_module = importlib.import_module(
        "agently.builtins.plugins.ExecutionResourceProvider.MCPExecutionResourceProvider"
    )
    provider = MCPExecutionResourceProvider()
    transport = object()

    with (
        mock.patch.dict(sys.modules, {"fastmcp": fake_fastmcp}),
        mock.patch.object(provider_module.LazyImport, "import_package") as lazy_import,
    ):
        handle = await provider.async_ensure(
            requirement={"kind": "mcp", "config": {"transport": transport}},
            policy={},
        )
        client = handle.get("resource")
        assert client is not None
        assert client.transport is transport
        assert await provider.async_health_check(handle) == "ready"
        await provider.async_release(handle)
        assert client.closed is True
        assert await provider.async_health_check(handle) == "unhealthy"

    lazy_import.assert_called_once_with("fastmcp", version_constraint=">=3", auto_install=False)


@pytest.mark.asyncio
async def test_action_use_mcp_shares_one_managed_session_across_registered_tools():
    import unittest.mock as mock

    tools = [
        types.SimpleNamespace(
            name=name,
            description=f"{name} description",
            inputSchema={"type": "object", "properties": {}},
            outputSchema={"type": "object", "properties": {}},
            _meta={},
        )
        for name in ("stateful_navigate", "stateful_snapshot")
    ]

    class FakeClient:
        def __init__(self, _transport):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def list_tools(self):
            return tools

    agent = Agently.create_agent()
    registrar_module = importlib.import_module("agently.core.operation.Action.ActionResourceRegistrar")
    with (
        mock.patch.object(registrar_module.LazyImport, "import_package"),
        mock.patch("fastmcp.Client", FakeClient),
    ):
        await agent.action.async_use_action_mcp("https://example.com/mcp")

    specs = [agent.action.action_registry.get_spec(tool.name) for tool in tools]
    requirements = [
        execution_resources[0]
        for spec in specs
        if spec is not None
        and (execution_resources := spec.get("execution_resources"))
    ]
    assert len(requirements) == 2
    assert len({item.get("resource_key") for item in requirements}) == 1
    assert str(requirements[0].get("resource_key", "")).startswith("mcp_server:")

def test_mcp_executor_resource_blocks_use_action_artifact_contract():
    from agently.builtins.plugins.ActionExecutor.MCPActionExecutor import MCPActionExecutor

    class FakeTextContent:
        def model_dump(self, **_kwargs):
            return {"type": "text", "text": "plain result"}

    class FakeResourceLink:
        def model_dump(self, **_kwargs):
            return {
                "type": "resource_link",
                "uri": "file:///tmp/agently/report.md",
                "name": "report.md",
                "mimeType": "text/markdown",
            }

    assert MCPActionExecutor._artifact_from_content_block(FakeTextContent()) is None

    artifact = MCPActionExecutor._artifact_from_content_block(FakeResourceLink())
    assert artifact is not None
    assert artifact["artifact_type"] == "mcp_resource_link"
    assert artifact["path"] == "file:///tmp/agently/report.md"
    assert artifact["media_type"] == "text/markdown"
    assert artifact["meta"]["source"] == "mcp"

    result = MCPActionExecutor._result_with_artifacts({"summary": "written"}, [artifact])
    assert result["status"] == "success"
    assert result["data"] == {"summary": "written"}
    assert result["artifacts"][0]["label"] == "report.md"


def test_mcp_executor_preserves_structured_explicit_artifact_refs():
    from agently.builtins.plugins.ActionExecutor.MCPActionExecutor import MCPActionExecutor

    structured = {
        "summary": "written",
        "artifact_refs": [
            {
                "path": "artifacts/report.md",
                "label": "report.md",
                "media_type": "text/markdown",
            }
        ],
    }

    result = MCPActionExecutor._result_with_artifacts(structured, [])
    assert result["status"] == "success"
    assert result["artifact_refs"] == structured["artifact_refs"]
    assert result["data"] is structured


def test_mcp_transport_normalization_supports_url_headers_and_configs():
    from agently.utils.MCP import normalize_mcp_transport

    normalized = normalize_mcp_transport(
        "https://example.com/mcp",
        headers={"Authorization": "Bearer token"},
    )
    assert normalized == {
        "mcpServers": {
            "default": {
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer token"},
            }
        }
    }

    config = {
        "mcpServers": {
            "weather": {"url": "https://weather.example/mcp"},
            "filesystem": {"command": "npx", "args": ["-y", "server"]},
        }
    }
    merged = normalize_mcp_transport(config, headers={"X-Team": "ops"})
    assert merged["mcpServers"]["weather"]["headers"] == {"X-Team": "ops"}
    assert "headers" not in merged["mcpServers"]["filesystem"]
    assert normalize_mcp_transport(config) is config


@pytest.mark.asyncio
async def test_docker_execution_resource_requires_daemon_preflight(monkeypatch):
    from agently.builtins.plugins.ExecutionResourceProvider import DockerExecutionResourceProvider
    docker_module = importlib.import_module(
        "agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider"
    )

    class FakeCompletedProcess:
        returncode = 1
        stdout = ""
        stderr = "Cannot connect to the Docker daemon"

    monkeypatch.setattr(docker_module.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(docker_module.subprocess, "run", lambda *args, **kwargs: FakeCompletedProcess())

    provider = DockerExecutionResourceProvider()

    with pytest.raises(ExecutionResourceError) as raised:
        await provider.async_ensure(
            requirement={
                "kind": "docker",
                "scope": "action_call",
                "resource_key": "docker_test",
                "config": {"docker_binary": "docker"},
            },
            policy={},
            existing_handle=None,
        )

    assert raised.value.code == "execution_resource.docker_unavailable"
    assert raised.value.payload["docker_binary"] == "docker"
    assert raised.value.payload["reason"] == "daemon_unavailable"


@pytest.mark.asyncio
async def test_docker_python_runtime_profile_uses_isolated_defaults(monkeypatch, tmp_path):
    from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
        DockerExecutionResource,
    )
    docker_module = importlib.import_module(
        "agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider"
    )

    calls: list[list[str]] = []

    class FakeCompletedProcess:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(args, **kwargs):
        _ = kwargs
        calls.append([str(item) for item in args])
        return FakeCompletedProcess()

    async def fake_bounded_process(args, **kwargs):
        _ = kwargs
        calls.append([str(item) for item in args])
        return BoundedProcessResult(
            returncode=0,
            stdout=b"ok\n",
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
        )

    monkeypatch.setattr(docker_module.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(docker_module.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_module, "run_bounded_process", fake_bounded_process)

    bundle, manifest, grant = await _materialized_code_bundle(
        tmp_path,
        language="python",
        source_code="print('ok')",
    )
    resource = DockerExecutionResource(
        docker_binary="docker",
        timeout=7,
        runtime_profile={
            "language": "python",
            "image": "python:3.12-slim",
            "network_mode": "disabled",
            "dependency_policy": {"mode": "deny"},
        },
        workspace_grant=grant,
    )

    result = await resource.async_execute_code(
        bundle=bundle,
        manifest=manifest,
        grant=grant,
        timeout=5,
    )

    assert result["ok"] is True
    args = calls[-1]
    assert args[:3] == ["docker", "run", "--rm"]
    assert "--network" in args
    assert args[args.index("--network") + 1] == "none"
    assert "--cpus" in args
    assert "--memory" in args
    assert any(arg.endswith(":/workspace/source:ro") for arg in args)
    assert "python:3.12-slim" in args
    image_index = args.index("python:3.12-slim")
    assert args[image_index + 1 : image_index + 3] == ["python", "main.py"]


@pytest.mark.asyncio
async def test_docker_runtime_profile_reports_timeout(monkeypatch, tmp_path):
    from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
        DockerExecutionResource,
    )
    docker_module = importlib.import_module(
        "agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider"
    )

    def fake_run(args, **kwargs):
        _ = kwargs
        if args[:3] == ["docker", "image", "inspect"]:
            return types.SimpleNamespace(returncode=0, stdout="sha256:python\n", stderr="")
        raise AssertionError(f"unexpected docker command: {args}")

    async def fake_bounded_process(args, **kwargs):
        _ = args, kwargs
        return BoundedProcessResult(
            returncode=124,
            stdout=b"partial stdout",
            stderr=b"partial stderr",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=True,
        )

    monkeypatch.setattr(docker_module.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(docker_module.subprocess, "run", fake_run)
    monkeypatch.setattr(docker_module, "run_bounded_process", fake_bounded_process)

    bundle, manifest, grant = await _materialized_code_bundle(
        tmp_path,
        language="python",
        source_code="import time; time.sleep(10)",
    )
    resource = DockerExecutionResource(
        docker_binary="docker",
        runtime_profile={
            "language": "python",
            "image": "python:3.12-slim",
        },
        workspace_grant=grant,
    )

    result = await resource.async_execute_code(
        bundle=bundle,
        manifest=manifest,
        grant=grant,
        timeout=3,
    )

    assert result["ok"] is False
    assert result["status"] == "timed_out"
    assert result["reason"] == "container_timeout"
    assert result["diagnostics"][0]["code"] == "docker_runtime.container_timeout"


@pytest.mark.asyncio
async def test_docker_shell_runtime_profile_keeps_command_allowlist(tmp_path):
    from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
        DockerExecutionResource,
    )

    resource = DockerExecutionResource(
        docker_binary="docker",
        runtime_profile={
            "language": "shell",
            "image": "python:3.12-slim",
            "allowed_cmd_prefixes": ["pwd"],
            "allowed_workdir_roots": [str(tmp_path)],
        },
    )

    blocked = await resource.run_shell_command(
        cmd="python -c 'print(1)'",
        workdir=str(tmp_path),
    )

    assert blocked["ok"] is False
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "cmd_not_allowed"
    assert blocked["diagnostics"][0]["code"] == "shell.cmd_not_allowed"


@pytest.mark.asyncio
async def test_docker_shell_runtime_normalizes_one_command_string_in_list(
    tmp_path,
    monkeypatch,
):
    from agently.builtins.plugins.ExecutionResourceProvider.DockerExecutionResourceProvider import (
        DockerExecutionResource,
    )

    captured: dict[str, Any] = {}

    async def fake_run_container(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    resource = DockerExecutionResource(
        docker_binary="docker",
        runtime_profile={
            "language": "shell",
            "image": "python:3.12-slim",
            "allowed_cmd_prefixes": ["python -m compileall"],
            "allowed_workdir_roots": [str(tmp_path)],
        },
    )
    monkeypatch.setattr(resource, "_run_container", fake_run_container)

    result = await resource.run_shell_command(
        cmd=["python -m compileall ."],
        workdir=str(tmp_path),
    )

    assert result["ok"] is True
    assert captured["cmd"] == ["python", "-m", "compileall", "."]


@pytest.mark.asyncio
async def test_action_use_mcp_url_headers_passes_normalized_transport_to_fastmcp():
    import unittest.mock as mock

    captured: list[Any] = []

    class FakeClient:
        def __init__(self, transport):
            captured.append(transport)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def list_tools(self):
            return []

    registrar_module = importlib.import_module("agently.core.operation.Action.ActionResourceRegistrar")

    with (
        mock.patch.object(registrar_module.LazyImport, "import_package") as lazy_import,
        mock.patch("fastmcp.Client", FakeClient),
    ):
        await Agently.action.async_use_mcp(
            "https://example.com/mcp",
            headers={"Authorization": "Bearer token"},
        )

    lazy_import.assert_called_once_with("fastmcp", version_constraint=">=3", auto_install=False)
    assert captured[-1]["mcpServers"]["default"]["url"] == "https://example.com/mcp"
    assert captured[-1]["mcpServers"]["default"]["headers"] == {"Authorization": "Bearer token"}


@pytest.mark.asyncio
async def test_action_use_mcp_rolls_back_batch_and_restores_host_action_on_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    import unittest.mock as mock

    tools = [
        types.SimpleNamespace(
            name="mcp_existing",
            description="MCP replacement.",
            inputSchema={"type": "object", "properties": {}},
            outputSchema={"type": "object", "properties": {}},
            _meta={},
        ),
        types.SimpleNamespace(
            name="mcp_new",
            description="New MCP Action.",
            inputSchema={"type": "object", "properties": {}},
            outputSchema={"type": "object", "properties": {}},
            _meta={},
        ),
    ]

    class FakeClient:
        def __init__(self, _transport):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def list_tools(self):
            return tools

    agent = Agently.create_agent()
    agent.action.register_action(
        action_id="mcp_existing",
        desc="Host-owned MCP Action.",
        kwargs={},
        func=lambda: {"owner": "host"},
    )
    original_register = agent.action.register_action
    registration_count = 0

    def fail_second_registration(**kwargs: Any):
        nonlocal registration_count
        registration_count += 1
        if registration_count == 2:
            raise RuntimeError("MCP batch registration failed")
        return original_register(**kwargs)

    monkeypatch.setattr(agent.action, "register_action", fail_second_registration)
    registrar_module = importlib.import_module("agently.core.operation.Action.ActionResourceRegistrar")

    with (
        mock.patch.object(registrar_module.LazyImport, "import_package"),
        mock.patch("fastmcp.Client", FakeClient),
        pytest.raises(RuntimeError, match="MCP batch registration failed"),
    ):
        await agent.action.async_use_mcp("https://example.com/mcp")

    restored = agent.action.action_registry.get_spec("mcp_existing")
    assert restored is not None
    assert restored.get("desc") == "Host-owned MCP Action."
    assert not agent.action.action_registry.has("mcp_new")


@pytest.mark.asyncio
async def test_action_use_mcp_rolls_back_when_client_exit_fails():
    import unittest.mock as mock

    tools = [
        types.SimpleNamespace(
            name="mcp_exit_existing",
            description="MCP replacement.",
            inputSchema={"type": "object", "properties": {}},
            outputSchema={"type": "object", "properties": {}},
            _meta={},
        ),
        types.SimpleNamespace(
            name="mcp_exit_new",
            description="New MCP Action.",
            inputSchema={"type": "object", "properties": {}},
            outputSchema={"type": "object", "properties": {}},
            _meta={},
        ),
    ]

    class FailingExitClient:
        def __init__(self, _transport):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            raise RuntimeError("MCP client exit failed")

        async def list_tools(self):
            return tools

    agent = Agently.create_agent()
    agent.action.register_action(
        action_id="mcp_exit_existing",
        desc="Host-owned MCP exit Action.",
        kwargs={},
        func=lambda: {"owner": "host"},
    )
    registrar_module = importlib.import_module("agently.core.operation.Action.ActionResourceRegistrar")

    with (
        mock.patch.object(registrar_module.LazyImport, "import_package"),
        mock.patch("fastmcp.Client", FailingExitClient),
        pytest.raises(RuntimeError, match="MCP client exit failed"),
    ):
        await agent.action.async_use_mcp("https://example.com/mcp")

    restored = agent.action.action_registry.get_spec("mcp_exit_existing")
    assert restored is not None
    assert restored.get("desc") == "Host-owned MCP exit Action."
    assert not agent.action.action_registry.has("mcp_exit_new")
