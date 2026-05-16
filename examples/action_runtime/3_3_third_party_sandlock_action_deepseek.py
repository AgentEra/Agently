import importlib.util
import sys
from typing import TYPE_CHECKING, Any

from _shared import create_deepseek_agent, print_action_results, print_response
from agently import Agently

if TYPE_CHECKING:
    from agently.types.data import ActionCall, ActionPolicy, ActionSpec
    from agently.utils import Settings


class SandLockActionExecutor:
    name = "SandLockActionExecutor"
    DEFAULT_SETTINGS = {}

    kind = "third_party_sandlock"
    sandboxed = True

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    @staticmethod
    def _decode_bytes(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value) if value is not None else ""

    async def execute(
        self,
        *,
        spec: "ActionSpec",
        action_call: "ActionCall",
        policy: "ActionPolicy",
        settings: "Settings",
    ) -> Any:
        _ = (spec, settings)

        if not sys.platform.startswith("linux"):
            raise RuntimeError("SandLock requires Linux. This example cannot run on the current platform.")

        try:
            from sandlock import Policy as SandLockPolicy  # type: ignore[reportMissingImports]
            from sandlock import Sandbox  # type: ignore[reportMissingImports]
        except ImportError as error:
            raise RuntimeError(
                "sandlock is not installed. Install it with `pip install sandlock` on Linux 6.7+."
            ) from error

        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}

        argv = action_input.get("argv", ["echo", "hello from sandlock"])
        if isinstance(argv, str):
            argv = [argv]
        if not isinstance(argv, list) or len(argv) == 0:
            raise ValueError("`argv` must be a non-empty list[str] or a single command string.")
        argv = [str(item) for item in argv]

        workdir = action_input.get("workdir")
        cwd = str(workdir) if workdir is not None else None

        timeout_seconds = policy.get("timeout_seconds", action_input.get("timeout_seconds", 10))
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            timeout_seconds = 10

        fs_readable = action_input.get(
            "fs_readable",
            ["/usr", "/bin", "/lib", "/lib64", "/etc", "/proc", "/dev"],
        )
        if not isinstance(fs_readable, list):
            fs_readable = [str(fs_readable)]
        fs_writable = action_input.get("fs_writable", ["/tmp"])
        if not isinstance(fs_writable, list):
            fs_writable = [str(fs_writable)]

        workspace_roots = policy.get("workspace_roots", [])
        if isinstance(workspace_roots, list):
            for path in workspace_roots:
                path_text = str(path)
                if path_text not in fs_readable:
                    fs_readable.append(path_text)
                if path_text not in fs_writable:
                    fs_writable.append(path_text)

        sandlock_policy = SandLockPolicy(
            fs_readable=[str(path) for path in fs_readable],
            fs_writable=[str(path) for path in fs_writable],
            cwd=cwd,
        )
        result = Sandbox(sandlock_policy).run(argv, timeout=float(timeout_seconds))
        return {
            "ok": bool(getattr(result, "success", False)),
            "returncode": getattr(result, "returncode", None),
            "stdout": self._decode_bytes(getattr(result, "stdout", b"")),
            "stderr": self._decode_bytes(getattr(result, "stderr", b"")),
        }


def register_sandlock_executor_plugin():
    plugin_list = Agently.plugin_manager.get_plugin_list("ActionExecutor")
    if "SandLockActionExecutor" not in plugin_list:
        Agently.plugin_manager.register("ActionExecutor", SandLockActionExecutor, activate=False)


if __name__ == "__main__":
    sandlock_available = importlib.util.find_spec("sandlock") is not None
    linux_platform = sys.platform.startswith("linux")

    if not sandlock_available or not linux_platform:
        print("[SKIPPED]")
        print("This example needs `sandlock` on Linux 6.7+.")
    else:
        register_sandlock_executor_plugin()

        agent = create_deepseek_agent(
            "Use the SandLock action for exact command execution inside the third-party sandbox."
        )
        agent.action.register_action(
            action_id="sandlock_exec",
            desc=(
                "Run a command with the third-party SandLock executor. "
                "Prefer short commands and return the observed stdout."
            ),
            kwargs={
                "argv": (list[str], "Command argv, for example ['echo', 'hello']."),
                "workdir": (str, "Optional working directory."),
                "fs_readable": (list[str], "Optional read allowlist."),
                "fs_writable": (list[str], "Optional write allowlist."),
                "timeout_seconds": (float, "Optional command timeout."),
            },
            executor=agent.action.create_action_executor("SandLockActionExecutor"),
            default_policy={
                "timeout_seconds": 10,
                "workspace_roots": [],
            },
            side_effect_level="exec",
            sandbox_required=True,
            expose_to_model=True,
        )

        agent.use_actions("sandlock_exec")
        agent.input(
            "Use the SandLock action to run `echo hello from third-party sandlock`, "
            "then tell me what the sandbox printed."
        )
        records = agent.get_action_result()
        print_action_results(records)
        response = agent.get_response()
        print_response(response)

# Expected key output after configuring DeepSeek on Linux 6.7+ with sandlock:
# [ACTION_RECORDS] includes a successful sandlock_exec call.
# The sandbox stdout contains "hello from third-party sandlock".
# If the platform or package is unavailable, the example prints [SKIPPED].
