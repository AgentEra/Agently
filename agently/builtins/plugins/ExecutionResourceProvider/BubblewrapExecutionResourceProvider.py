# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
BubblewrapExecutionResourceProvider — Linux bwrap sandbox backend.

Uses ``bwrap`` (bubblewrap) to provide user-namespace sandboxing on Linux.
Bubblewrap is the same tool used by Flatpak and is available in most
Linux distributions.

This provider conforms to the Agently 4.1.4.2 ExecutionResourceProvider
contract: it registers under ``kind="code_execution"`` and implements
``async_probe`` / ``async_ensure`` / ``async_health_check`` /
``async_release`` / ``async_execute_code``.

Only functional on Linux.  On other platforms the provider reports itself
as unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from agently.types.data import (
    CodeExecutionBundle,
    TaskWorkspaceAccessGrant,
    TaskWorkspaceExecutionManifest,
    resolve_code_execution_workspace_uri,
)
from agently.types.data.code_execution import extract_code_toolchain_version

from ._bounded_process import run_bounded_process


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def is_linux() -> bool:
    return platform.system() == "Linux"


# ---------------------------------------------------------------------------
# Availability probe
# ---------------------------------------------------------------------------

def inspect_bubblewrap_availability() -> dict[str, Any]:
    """Check whether bwrap is usable on this system."""
    if not is_linux():
        return {"available": False, "reason": "not_linux"}
    binary = shutil.which("bwrap")
    if binary is None:
        return {"available": False, "reason": "bwrap_binary_missing"}
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        version = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "unknown"
    except Exception as error:
        return {"available": False, "reason": "bwrap_version_failed", "error": str(error)}
    return {
        "available": True,
        "binary": binary,
        "platform": "linux",
        "version": version,
    }


# ---------------------------------------------------------------------------
# BubblewrapCodeExecutionResource
# ---------------------------------------------------------------------------

class BubblewrapCodeExecutionResource:
    """Execute code inside a Linux bwrap sandbox.

    Follows the same bundle/manifest/grant validation pattern as
    TrustedLocalCodeExecutionResource, but wraps each execution step
    with ``bwrap`` to provide user-namespace isolation.
    """

    def __init__(
        self,
        *,
        grant: TaskWorkspaceAccessGrant,
        max_output_bytes: int = 20000,
        network: bool = False,
        bind_ro: list[str] | None = None,
        bind_rw: list[str] | None = None,
        tmpfs: list[str] | None = None,
        unshare_all: bool = True,
        share_net: bool = False,
        clearenv: bool = False,
        new_session: bool = True,
        die_with_parent: bool = True,
        extra_bwrap_args: list[str] | None = None,
    ) -> None:
        self.grant = grant
        self.max_output_bytes = max(1, int(max_output_bytes))
        self.network = network
        self.bind_ro = [str(p) for p in (bind_ro or [])]
        self.bind_rw = [str(p) for p in (bind_rw or [])]
        self.tmpfs = [str(p) for p in (tmpfs or [])]
        self.unshare_all = unshare_all
        self.share_net = share_net
        self.clearenv = clearenv
        self.new_session = new_session
        self.die_with_parent = die_with_parent
        self.extra_bwrap_args = list(extra_bwrap_args or [])
        self._active_executions: set[asyncio.Task[Any]] = set()
        self._closed = False

    @staticmethod
    def _sha256(path: Path) -> str:
        return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"

    def _validate_materialization(
        self,
        *,
        bundle: CodeExecutionBundle,
        manifest: TaskWorkspaceExecutionManifest,
        grant: TaskWorkspaceAccessGrant,
    ) -> Path:
        if grant != self.grant:
            raise PermissionError("Bubblewrap resource is bound to another Workspace grant.")
        if (
            manifest.grant_id != grant.grant_id
            or manifest.bundle_id != bundle.bundle_id
            or manifest.bundle_digest != bundle.bundle_digest
        ):
            raise PermissionError("Code execution manifest does not match the bound bundle and grant.")
        area = Path(grant.execution_area).resolve()
        manifest_files = {Path(item.host_path).resolve(): item for item in manifest.files}
        for item in bundle.files:
            target = (area / "source" / Path(item.path)).resolve()
            if area not in target.parents or target.is_symlink() or not target.is_file():
                raise PermissionError("Materialized bundle file escaped or is unavailable.")
            recorded = manifest_files.get(target)
            if recorded is None or recorded.sha256 != item.sha256:
                raise PermissionError("Materialized bundle file is absent from the Workspace manifest.")
            if self._sha256(target) != item.sha256:
                raise PermissionError("Materialized bundle file digest changed before execution.")
        return area

    def _build_bwrap_argv(self, user_argv: list[str], *, area: Path) -> list[str]:
        """Construct the full bwrap argv prefix + user command."""
        args: list[str] = ["bwrap"]

        # Namespace isolation
        if self.unshare_all:
            args.append("--unshare-all")
            if self.share_net or self.network:
                args.append("--share-net")
        else:
            args.extend(["--unshare-user", "--unshare-pid"])

        if self.die_with_parent:
            args.append("--die-with-parent")
        if self.new_session:
            args.append("--new-session")
        if self.clearenv:
            args.append("--clearenv")

        # Default system bind mounts (read-only)
        default_ro = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/alternatives"]
        mounted_dests: set[str] = set()
        for src in default_ro:
            if Path(src).exists():
                args.extend(["--ro-bind", src, src])
                mounted_dests.add(src)

        # User-configured read-only binds
        for src in self.bind_ro:
            args.extend(["--ro-bind", src, src])
            mounted_dests.add(src)

        # User-configured read-write binds
        for src in self.bind_rw:
            args.extend(["--bind", src, src])
            mounted_dests.add(src)

        # Workspace grant roots
        for root in self.grant.roots:
            if root.access_mode == "read_write":
                args.extend(["--bind", root.host_path, root.host_path])
            else:
                args.extend(["--ro-bind", root.host_path, root.host_path])
            mounted_dests.add(root.host_path)

        # Tmpfs mounts
        for mount_point in self.tmpfs:
            args.extend(["--tmpfs", mount_point])

        # Default /proc and /dev if not already mounted
        if "/proc" not in mounted_dests:
            args.extend(["--proc", "/proc"])
        if "/dev" not in mounted_dests:
            args.extend(["--dev", "/dev"])

        # Extra args
        args.extend(self.extra_bwrap_args)

        # User command
        args.extend(user_argv)
        return args

    async def _run(
        self,
        *,
        bundle: CodeExecutionBundle,
        area: Path,
        timeout: int,
    ) -> dict[str, Any]:
        logs_root = area / "logs"
        logs_root.mkdir(parents=True, exist_ok=True)
        steps = (*bundle.build_steps, bundle.run_step)
        final_stdout = b""
        final_stderr = b""
        returncode = 0
        log_refs: list[str] = []
        for index, step in enumerate(steps):
            cwd = (area / Path(step.cwd)).resolve()
            if area not in cwd.parents or not cwd.is_dir() or cwd.is_symlink():
                raise PermissionError("Execution step cwd escaped its Workspace grant.")
            stdout_path = logs_root / f"{index:02d}-{step.role}.stdout.log"
            stderr_path = logs_root / f"{index:02d}-{step.role}.stderr.log"
            environment = dict(os.environ)
            workspace_roots = {
                root.role: root.host_path
                for root in self.grant.roots
                if root.role in {"source", "build", "output", "logs"}
            }
            environment.update(
                {
                    key: resolve_code_execution_workspace_uri(
                        value,
                        roots=workspace_roots,
                    )
                    for key, value in step.env.items()
                }
            )
            argv = self._build_bwrap_argv(list(step.argv), area=area)
            completed = await run_bounded_process(
                argv,
                cwd=str(cwd),
                env=environment,
                timeout=max(1, timeout),
                max_output_bytes=self.max_output_bytes,
            )
            returncode = completed.returncode
            final_stdout = completed.stdout
            final_stderr = completed.stderr
            if completed.timed_out:
                timeout_message = (
                    f"execution timed out after {timeout} seconds\n".encode()
                )
                remaining = max(0, self.max_output_bytes - len(final_stderr))
                final_stderr += timeout_message[:remaining]
            stdout_path.write_bytes(final_stdout)
            stderr_path.write_bytes(final_stderr)
            log_refs.extend(
                [
                    f"logs/{stdout_path.name}",
                    f"logs/{stderr_path.name}",
                ]
            )
            if returncode != 0:
                break
        outputs = [
            path
            for path in bundle.expected_outputs
            if (area / Path(path)).is_file() and not (area / Path(path)).is_symlink()
        ]
        stdout_truncated = completed.stdout_truncated
        stderr_truncated = completed.stderr_truncated or completed.timed_out
        return {
            "ok": returncode == 0,
            "status": "success" if returncode == 0 else "error",
            "returncode": returncode,
            "stdout": final_stdout.decode("utf-8", errors="replace"),
            "stderr": final_stderr.decode("utf-8", errors="replace"),
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "outputs": outputs,
            "log_refs": log_refs,
        }

    async def async_execute_code(
        self,
        *,
        bundle: CodeExecutionBundle,
        manifest: TaskWorkspaceExecutionManifest,
        grant: TaskWorkspaceAccessGrant,
        timeout: int,
    ) -> dict[str, Any]:
        if self._closed:
            raise RuntimeError("Bubblewrap execution resource is closed.")
        area = self._validate_materialization(
            bundle=bundle,
            manifest=manifest,
            grant=grant,
        )
        task = asyncio.current_task()
        if task is not None:
            self._active_executions.add(task)
        try:
            return await self._run(
                bundle=bundle,
                area=area,
                timeout=timeout,
            )
        finally:
            if task is not None:
                self._active_executions.discard(task)

    async def async_close(self) -> None:
        self._closed = True
        current = asyncio.current_task()
        active = [task for task in self._active_executions if task is not current]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)


# ---------------------------------------------------------------------------
# BubblewrapExecutionResourceProvider
# ---------------------------------------------------------------------------

class BubblewrapExecutionResourceProvider:
    """Provider that creates bwrap-sandboxed execution resources.

    Conforms to the 4.1.4.2 ExecutionResourceProvider contract:
    - ``provider_id = "bubblewrap"``
    - ``supported_kinds = ("code_execution",)``
    - Implements ``async_probe`` / ``async_ensure`` / ``async_health_check``
      / ``async_release``
    """

    name = "BubblewrapExecutionResourceProvider"
    provider_id = "bubblewrap"
    supported_kinds = ("code_execution",)

    @staticmethod
    def _on_register() -> None:
        return None

    @staticmethod
    def _on_unregister() -> None:
        return None

    @staticmethod
    def _tool_facts() -> dict[str, dict[str, Any]]:
        commands = {
            "python": ("python3", ("--version",)),
        }
        facts: dict[str, dict[str, Any]] = {}
        for language, (tool, command_args) in commands.items():
            binary = shutil.which(tool)
            fact: dict[str, Any] = {
                "tool": tool,
                "available": binary is not None,
                "binary": binary or "",
                "version": "",
                "raw_version": "",
            }
            if binary is not None:
                try:
                    completed = subprocess.run(
                        [binary, *command_args],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    raw_version = str(completed.stdout or completed.stderr).strip()[:300]
                    fact["raw_version"] = raw_version
                    fact["version"] = extract_code_toolchain_version(raw_version)
                    fact["available"] = completed.returncode == 0
                except Exception as error:
                    fact.update(available=False, error=str(error)[:300])
            facts[language] = fact
        return facts

    async def async_probe(self, *, requirement, policy):
        _ = requirement, policy
        availability = await asyncio.to_thread(inspect_bubblewrap_availability)
        available = bool(availability.get("available"))
        reason = str(availability.get("reason", "available")) if not available else "bubblewrap available"
        facts = await asyncio.to_thread(self._tool_facts) if available else {}
        languages = [language for language, fact in facts.items() if fact["available"]]
        toolchains = {
            str(fact["tool"]): {
                "available": bool(fact["available"]),
                "version": str(fact.get("version", "")),
                "raw_version": str(fact.get("raw_version", "")),
                "binary": str(fact.get("binary", "")),
            }
            for fact in facts.values()
        }
        return {
            "provider_id": self.provider_id,
            "available": available and bool(languages),
            "supported_kinds": list(self.supported_kinds),
            "capabilities": {
                "languages": languages,
                "toolchains": toolchains,
                "isolation": {
                    "process_contained": True,
                    "host_filesystem_restricted": True,
                    "privilege_escalation_blocked": True,
                    "syscalls_restricted": False,
                    "mechanism": "bubblewrap",
                    "network_mode": "configurable",
                },
                "workspace_access_modes": ["snapshot", "read_only", "read_write"],
                "network": "configurable",
                "safety_class": "isolated",
            },
            "reason": reason,
            "meta": {"availability": availability, "toolchains": facts},
        }

    async def async_ensure(self, *, requirement, policy):
        from agently.core import ExecutionResourceError

        config = requirement.get("config", {})
        config = config if isinstance(config, dict) else {}
        grant = requirement.get("task_workspace_access_grant")
        if str(requirement.get("kind", "")) == "code_execution":
            if not isinstance(grant, TaskWorkspaceAccessGrant):
                raise ExecutionResourceError(
                    "Bubblewrap code execution requires a TaskWorkspace access grant.",
                    code="execution_resource.workspace_grant_required",
                    payload={"provider_id": self.provider_id},
                )

        availability = await asyncio.to_thread(inspect_bubblewrap_availability)
        if not availability.get("available"):
            raise ExecutionResourceError(
                f"Bubblewrap is not available: {availability.get('reason', 'unknown')}",
                code="execution_resource.bubblewrap_unavailable",
                payload={"provider_id": self.provider_id, "availability": availability},
            )

        return {
            "handle_id": f"bubblewrap:{uuid.uuid4().hex}",
            "resource": BubblewrapCodeExecutionResource(
                grant=grant,
                max_output_bytes=int(policy.get("max_output_bytes", 20000)),
                network=bool(config.get("network", False)),
                bind_ro=[str(p) for p in config.get("bind_ro", [])],
                bind_rw=[str(p) for p in config.get("bind_rw", [])],
                tmpfs=[str(p) for p in config.get("tmpfs", [])],
                unshare_all=bool(config.get("unshare_all", True)),
                share_net=bool(config.get("share_net", False)),
                clearenv=bool(config.get("clearenv", False)),
                new_session=bool(config.get("new_session", True)),
                die_with_parent=bool(config.get("die_with_parent", True)),
                extra_bwrap_args=[str(a) for a in config.get("extra_bwrap_args", [])],
            ),
            "status": "ready",
            "meta": {
                "provider": self.name,
                "available": True,
                "platform": "linux",
                "grant_id": grant.grant_id if isinstance(grant, TaskWorkspaceAccessGrant) else None,
            },
        }

    async def async_health_check(self, handle):
        return "ready" if isinstance(
            handle.get("resource"), BubblewrapCodeExecutionResource
        ) else "unhealthy"

    async def async_release(self, handle) -> None:
        resource = handle.get("resource")
        if isinstance(resource, BubblewrapCodeExecutionResource):
            await resource.async_close()


__all__ = [
    "BubblewrapCodeExecutionResource",
    "BubblewrapExecutionResourceProvider",
    "inspect_bubblewrap_availability",
]
