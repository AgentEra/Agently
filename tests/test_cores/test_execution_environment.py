import pytest
from typing import Any, cast

from agently import Agently
from agently.core import ExecutionEnvironmentApprovalRequired, ExecutionEnvironmentError, ExecutionEnvironmentManager
from agently.types.data import ExecutionEnvironmentRequirement
from agently.utils import Settings


def _create_manager():
    settings = Settings(name="ExecutionEnvironmentTestSettings", parent=Agently.settings)
    return ExecutionEnvironmentManager(
        plugin_manager=Agently.plugin_manager,
        settings=settings,
        event_center=Agently.event_center,
    )


def test_execution_environment_declare_is_lazy():
    manager = _create_manager()
    requirement = manager.declare(
        {
            "kind": "python",
            "scope": "action_call",
            "resource_key": "python_test",
        }
    )

    assert requirement["kind"] == "python"
    assert manager.list() == []


@pytest.mark.asyncio
async def test_execution_environment_ensure_reuses_and_releases_handle():
    manager = _create_manager()
    requirement = cast(ExecutionEnvironmentRequirement, {
        "kind": "python",
        "scope": "session",
        "owner_id": "session-1",
        "resource_key": "python_test",
        "config": {"base_vars": {"value": 1}},
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
async def test_execution_environment_rechecks_health_before_reuse():
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
    requirement = cast(ExecutionEnvironmentRequirement, {
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
async def test_execution_environment_approval_required_does_not_start():
    manager = _create_manager()

    with pytest.raises(ExecutionEnvironmentApprovalRequired):
        await manager.async_ensure(
            {
                "kind": "python",
                "scope": "action_call",
                "resource_key": "python_test",
                "approval_required": True,
            }
        )

    assert manager.list() == []


def test_action_python_sandbox_uses_execution_environment():
    action_id = "python_env_action"
    Agently.action.register_python_sandbox_action(action_id=action_id, expose_to_model=False)

    result = Agently.action.execute_action(action_id, {"python_code": "result = 40 + 2"})

    assert result.get("status") == "success"
    result_data = cast(dict[str, Any], result.get("data"))
    assert result_data["result"] == 42
    assert Agently.execution_environment.list(scope="action_call") == []


def test_action_bash_sandbox_uses_execution_environment(tmp_path):
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
    assert Agently.execution_environment.list(scope="action_call") == []


def test_action_environment_approval_returns_action_result():
    action_id = "approval_env_action"
    Agently.action.register_python_sandbox_action(action_id=action_id, expose_to_model=False)
    spec = Agently.action.action_registry.get_spec(action_id)
    assert spec is not None
    spec.get("execution_environments", [])[0]["approval_required"] = True

    result = Agently.action.execute_action(action_id, {"python_code": "result = 1"})

    assert result.get("status") == "approval_required"
    approval = cast(dict[str, Any], result.get("approval"))
    assert approval["required"] is True


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
async def test_execution_environment_release_scope_cleans_handles():
    manager = _create_manager()
    owner = "scope-test-owner"

    await manager.async_ensure(
        {"kind": "python", "scope": "agent", "owner_id": owner, "resource_key": "py1"},
    )
    await manager.async_ensure(
        {"kind": "python", "scope": "session", "owner_id": owner, "resource_key": "py2"},
    )
    await manager.async_ensure(
        {"kind": "python", "scope": "agent", "owner_id": "other-owner", "resource_key": "py3"},
    )

    assert len(manager.list(scope="agent", owner_id=owner)) == 1
    assert len(manager.list(scope="session", owner_id=owner)) == 1

    await manager.async_release_scope("agent", owner)

    assert manager.list(scope="agent", owner_id=owner) == []
    assert len(manager.list(scope="session", owner_id=owner)) == 1
    assert len(manager.list(scope="agent", owner_id="other-owner")) == 1

    await manager.async_release_scope("session", owner)
    assert manager.list(scope="session", owner_id=owner) == []


@pytest.mark.asyncio
async def test_execution_environment_missing_provider_raises_stable_error():
    manager = _create_manager()

    with pytest.raises(ExecutionEnvironmentError) as exc_info:
        await manager.async_ensure(
            {"kind": "nonexistent_provider_xyz", "scope": "action_call", "resource_key": "nope"},
        )

    error = exc_info.value
    assert hasattr(error, "code")
    assert error.code == "execution_environment.provider_missing"
    assert manager.list() == []


@pytest.mark.asyncio
async def test_execution_environment_approval_denied_returns_blocked_action_result():
    action_id = "denied_approval_env_action"
    Agently.action.register_python_sandbox_action(action_id=action_id, expose_to_model=False)
    spec = Agently.action.action_registry.get_spec(action_id)
    assert spec is not None
    spec.get("execution_environments", [])[0]["approval_required"] = True

    Agently.execution_environment.set_decision_handler(lambda req, pol: False)
    try:
        result = Agently.action.execute_action(action_id, {"python_code": "result = 1"})
    finally:
        Agently.execution_environment.set_decision_handler(None)

    assert result.get("status") == "blocked"
    assert Agently.execution_environment.list() == []


@pytest.mark.asyncio
async def test_execution_environment_provider_failure_does_not_poison_registry():
    from agently.core.ExecutionEnvironment import ExecutionEnvironmentManager
    from agently.utils import Settings

    settings = Settings(name="FailProviderTestSettings", parent=Agently.settings)
    manager = ExecutionEnvironmentManager(
        plugin_manager=Agently.plugin_manager,
        settings=settings,
        event_center=Agently.event_center,
    )

    class FailingProvider:
        name = "FailingProvider"
        kind = "python"
        DEFAULT_SETTINGS: dict[str, Any] = {}

        @staticmethod
        def _on_register():
            pass

        @staticmethod
        def _on_unregister():
            pass

        async def async_ensure(self, *, requirement, policy, existing_handle=None):
            raise RuntimeError("Simulated provider failure")

        async def async_health_check(self, handle):
            return "unhealthy"

        async def async_release(self, handle):
            pass

    manager.register_provider(FailingProvider())

    declared = manager.declare(
        {"kind": "python", "scope": "action_call", "resource_key": "fail_test"}
    )
    requirement_id = declared.get("requirement_id")
    assert requirement_id is not None

    with pytest.raises(RuntimeError, match="Simulated provider failure"):
        await manager.async_ensure({"kind": "python", "scope": "action_call", "resource_key": "fail_test"})

    assert manager.list() == []
    assert manager.inspect(requirement_id) is not None


@pytest.mark.asyncio
async def test_mcp_executor_transport_routing():
    import unittest.mock as mock
    from agently.builtins.plugins.ActionExecutor.MCPActionExecutor import MCPActionExecutor

    direct_transport = object()
    managed_transport = object()
    executor = MCPActionExecutor(action_id="my_tool", transport=direct_transport)

    captured: list[Any] = []

    def fake_client(transport):
        captured.append(transport)
        ctx = mock.MagicMock()
        ctx.__aenter__ = mock.AsyncMock(side_effect=Exception("stop"))
        ctx.__aexit__ = mock.AsyncMock(return_value=False)
        return ctx

    spec = {"action_id": "my_tool"}
    policy: dict[str, Any] = {}
    settings = mock.MagicMock()

    with mock.patch("fastmcp.Client", fake_client):
        action_call_no_env: dict[str, Any] = {
            "action_input": {},
            "execution_environment_resources": {},
        }
        try:
            await executor.execute(spec=spec, action_call=action_call_no_env, policy=policy, settings=settings)
        except Exception:
            pass
        assert captured and captured[-1] is direct_transport, \
            "Without managed resource, executor must use direct transport"

        captured.clear()

        action_call_with_env: dict[str, Any] = {
            "action_input": {},
            "execution_environment_resources": {"my_tool": managed_transport},
        }
        try:
            await executor.execute(spec=spec, action_call=action_call_with_env, policy=policy, settings=settings)
        except Exception:
            pass
        assert captured and captured[-1] is managed_transport, \
            "With managed resource injected, executor must prefer managed transport"
