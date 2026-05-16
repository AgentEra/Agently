import asyncio
import shutil
import subprocess
import uuid
from typing import TYPE_CHECKING, Any

from _shared import create_deepseek_agent, print_action_results, print_response
from agently import Agently

if TYPE_CHECKING:
    from agently.types.data import ActionCall, ActionPolicy, ActionSpec
    from agently.utils import Settings


DEFAULT_DOCKER_IMAGE = "alpine:3.20"


class DockerSandboxActionExecutor:
    name = "DockerSandboxActionExecutor"
    DEFAULT_SETTINGS = {}

    kind = "third_party_docker_sandbox"
    sandboxed = True

    def __init__(
        self,
        *,
        image: str = DEFAULT_DOCKER_IMAGE,
        auto_pull: bool = True,
        memory: str = "128m",
        cpus: str = "1",
        pids_limit: int = 64,
        pull_timeout_seconds: int = 180,
    ):
        self.image = image
        self.auto_pull = auto_pull
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.pull_timeout_seconds = pull_timeout_seconds

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

    async def _run_docker_cli(self, args: list[str], *, timeout_seconds: float):
        process = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout = b""
        stderr = b""
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            return {
                "ok": False,
                "returncode": None,
                "stdout": self._decode_bytes(stdout),
                "stderr": self._decode_bytes(stderr) + f"\nDocker command timed out after { timeout_seconds } seconds.",
            }
        return {
            "ok": process.returncode == 0,
            "returncode": process.returncode,
            "stdout": self._decode_bytes(stdout),
            "stderr": self._decode_bytes(stderr),
        }

    async def _ensure_image(self):
        inspect_result = await self._run_docker_cli(
            ["image", "inspect", self.image],
            timeout_seconds=10,
        )
        if inspect_result["ok"]:
            return
        if not self.auto_pull:
            raise RuntimeError(
                f"Docker image `{ self.image }` is not available locally. "
                "Pull it first or create the executor with auto_pull=True."
            )
        pull_result = await self._run_docker_cli(
            ["pull", self.image],
            timeout_seconds=float(self.pull_timeout_seconds),
        )
        if not pull_result["ok"]:
            raise RuntimeError(
                f"Failed to pull Docker image `{ self.image }`: { pull_result['stderr'] or pull_result['stdout'] }"
            )

    async def execute(
        self,
        *,
        spec: "ActionSpec",
        action_call: "ActionCall",
        policy: "ActionPolicy",
        settings: "Settings",
    ) -> Any:
        _ = (spec, settings)

        if shutil.which("docker") is None:
            raise RuntimeError("Docker CLI is not installed or is not on PATH.")

        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}

        cmd = str(action_input.get("cmd", "")).strip()
        if cmd == "":
            raise ValueError("`cmd` must be a non-empty shell command string.")

        timeout_seconds = policy.get("timeout_seconds", action_input.get("timeout_seconds", 10))
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            timeout_seconds = 10

        await self._ensure_image()

        container_name = f"agently-action-sandbox-{ uuid.uuid4().hex[:12] }"
        network_mode = str(policy.get("network_mode", "disabled")).lower()
        docker_network = "none" if network_mode == "disabled" else "bridge"

        run_args = [
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            docker_network,
            "--cpus",
            self.cpus,
            "--memory",
            self.memory,
            "--pids-limit",
            str(self.pids_limit),
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=16m",
            "--workdir",
            "/tmp",
            "--env",
            "HOME=/tmp",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            self.image,
            "sh",
            "-lc",
            cmd,
        ]
        result = await self._run_docker_cli(
            run_args,
            timeout_seconds=float(timeout_seconds),
        )
        if result["returncode"] is None:
            await self._run_docker_cli(["rm", "-f", container_name], timeout_seconds=5)
        result["image"] = self.image
        result["network"] = docker_network
        return result


def docker_unavailable_reason(image: str = DEFAULT_DOCKER_IMAGE):
    if shutil.which("docker") is None:
        return "Docker CLI is not installed or is not on PATH."

    try:
        info_result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "Docker daemon did not respond to `docker info` within 10 seconds."
    if info_result.returncode != 0:
        return info_result.stderr.strip() or info_result.stdout.strip() or "Docker daemon is not running."

    inspect_result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if inspect_result.returncode == 0:
        return None

    print(f"[SETUP] Pulling Docker image { image } ...")
    try:
        pull_result = subprocess.run(
            ["docker", "pull", image],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"Timed out while pulling Docker image `{ image }`."
    if pull_result.returncode != 0:
        return pull_result.stderr.strip() or pull_result.stdout.strip() or f"Failed to pull Docker image `{ image }`."
    return None


def register_docker_sandbox_executor_plugin():
    plugin_list = Agently.plugin_manager.get_plugin_list("ActionExecutor")
    if "DockerSandboxActionExecutor" not in plugin_list:
        Agently.plugin_manager.register("ActionExecutor", DockerSandboxActionExecutor, activate=False)


if __name__ == "__main__":
    unavailable_reason = docker_unavailable_reason()
    if unavailable_reason is not None:
        print("[SKIPPED]")
        print(unavailable_reason)
    else:
        register_docker_sandbox_executor_plugin()

        agent = create_deepseek_agent(
            "Use the Docker sandbox action for exact command execution inside an ephemeral local container."
        )
        agent.action.register_action(
            action_id="docker_sandbox_exec",
            desc=(
                "Run a short POSIX shell command in an ephemeral Docker container. "
                "The container has no network, a read-only root filesystem, and a writable /tmp."
            ),
            kwargs={
                "cmd": (str, "Short POSIX shell command to run inside the container."),
                "timeout_seconds": (float, "Optional command timeout."),
            },
            executor=agent.action.create_action_executor("DockerSandboxActionExecutor"),
            default_policy={
                "timeout_seconds": 10,
                "network_mode": "disabled",
            },
            side_effect_level="exec",
            sandbox_required=True,
            expose_to_model=True,
        )

        agent.use_actions("docker_sandbox_exec")
        agent.input(
            "Use the Docker sandbox action to run `printf 'hello from docker sandbox\\n'` exactly once, "
            "then tell me exactly what the container printed."
        )
        records = agent.get_action_result()
        print_action_results(records)
        response = agent.get_response()
        print_response(response)

# Expected key output after configuring DeepSeek with Docker running:
# [ACTION_RECORDS] includes a successful docker_sandbox_exec call.
# The container stdout contains "hello from docker sandbox".
# If Docker is unavailable, the example prints [SKIPPED].
