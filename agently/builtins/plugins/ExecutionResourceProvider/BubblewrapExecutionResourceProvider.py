# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Grant-bound Linux Bubblewrap code-execution provider."""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agently.types.data import (
    CodeExecutionBundle,
    TaskWorkspaceAccessGrant,
    TaskWorkspaceExecutionManifest,
    resolve_code_execution_workspace_uri,
)
from agently.types.data.code_execution import extract_code_toolchain_version

from ._bounded_process import run_bounded_process

if TYPE_CHECKING:
    from agently.types.data import (
        ExecutionResourceHandle,
        ExecutionResourcePolicy,
        ExecutionResourceProviderProbe,
        ExecutionResourceRequirement,
        ExecutionResourceStatus,
    )


def is_linux() -> bool:
    return platform.system() == "Linux"


def _system_read_roots() -> list[str]:
    return [
        path
        for path in (
            "/usr",
            "/bin",
            "/sbin",
            "/lib",
            "/lib64",
            "/etc/alternatives",
            "/etc/ssl",
        )
        if Path(path).exists()
    ]


def _append_ro_bind(args: list[str], path: str, mounted: set[str]) -> None:
    resolved = str(Path(path).resolve())
    if resolved in mounted or not Path(resolved).exists():
        return
    args.extend(["--ro-bind", resolved, resolved])
    mounted.add(resolved)


def _mechanism_argv(binary: str) -> list[str]:
    args = [binary, "--unshare-all", "--die-with-parent", "--new-session"]
    mounted: set[str] = set()
    for path in _system_read_roots():
        _append_ro_bind(args, path, mounted)
    args.extend(["--proc", "/proc", "--dev", "/dev", "/usr/bin/true"])
    return args


def inspect_bubblewrap_availability() -> dict[str, Any]:
    """Run a real bounded namespace/mount probe."""

    if not is_linux():
        return {"available": False, "reason": "not_linux"}
    binary = shutil.which("bwrap")
    if binary is None:
        return {"available": False, "reason": "bwrap_binary_missing"}
    try:
        version = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        mechanism = subprocess.run(
            _mechanism_argv(binary),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "available": False,
            "reason": "bwrap_mechanism_failed",
            "binary": binary,
            "error": str(error)[:300],
        }
    if version.returncode != 0 or mechanism.returncode != 0:
        return {
            "available": False,
            "reason": "bwrap_user_namespace_blocked",
            "binary": binary,
            "version_output": str(version.stdout or version.stderr).strip()[:300],
            "returncode": mechanism.returncode,
            "stdout": mechanism.stdout[:300],
            "stderr": mechanism.stderr[:300],
        }
    return {
        "available": True,
        "reason": "ready",
        "binary": binary,
        "version": str(version.stdout or version.stderr).strip()[:300],
        "platform": "linux",
    }


class BubblewrapCodeExecutionResource:
    """Execute immutable code bundles in provider-owned Linux namespaces."""

    def __init__(
        self,
        *,
        grant: TaskWorkspaceAccessGrant,
        max_output_bytes: int = 20000,
        network: bool = False,
    ) -> None:
        self.grant = grant
        self.max_output_bytes = max(1, int(max_output_bytes))
        self.network = bool(network)
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

    def _root_map(self) -> dict[str, str]:
        return {
            root.role: root.host_path
            for root in self.grant.roots
            if root.role in {"source", "build", "output", "logs"}
        }

    @staticmethod
    def _toolchain_root(command: str) -> str | None:
        binary = shutil.which(command)
        if binary is None:
            return None
        path = Path(binary).resolve()
        if str(path).startswith("/usr/"):
            return "/usr"
        return str(path.parent.parent)

    def _build_bwrap_argv(
        self,
        user_argv: list[str],
        *,
        area: Path,
        environment: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> list[str]:
        binary = shutil.which("bwrap") or "bwrap"
        args = [binary, "--unshare-all", "--die-with-parent", "--new-session"]
        if self.network:
            args.append("--share-net")
        mounted: set[str] = set()
        for path in _system_read_roots():
            _append_ro_bind(args, path, mounted)
        if user_argv:
            toolchain_root = self._toolchain_root(user_argv[0])
            if toolchain_root is not None:
                _append_ro_bind(args, toolchain_root, mounted)
        for root in self.grant.roots:
            path = str(Path(root.host_path).resolve())
            if root.access_mode == "read_write":
                args.extend(["--bind", path, path])
            else:
                args.extend(["--ro-bind", path, path])
            mounted.add(path)
        args.extend(["--proc", "/proc", "--dev", "/dev", "--clearenv"])
        child_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        roots = self._root_map()
        temp_root = roots.get("build") or roots.get("logs")
        if temp_root:
            child_env.update(TMPDIR=temp_root, TMP=temp_root, TEMP=temp_root)
        child_env.update(environment or {})
        for key, value in child_env.items():
            args.extend(["--setenv", str(key), str(value)])
        args.extend(["--chdir", cwd or str(area), *user_argv])
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
        roots = self._root_map()
        final_stdout = b""
        final_stderr = b""
        returncode = 0
        stdout_truncated = False
        stderr_truncated = False
        log_refs: list[str] = []
        for index, step in enumerate((*bundle.build_steps, bundle.run_step)):
            cwd = (area / Path(step.cwd)).resolve()
            if area not in cwd.parents or not cwd.is_dir() or cwd.is_symlink():
                raise PermissionError("Execution step cwd escaped its Workspace grant.")
            environment = {
                key: resolve_code_execution_workspace_uri(value, roots=roots)
                for key, value in step.env.items()
            }
            argv = self._build_bwrap_argv(
                list(step.argv),
                area=area,
                environment=environment,
                cwd=str(cwd),
            )
            completed = await run_bounded_process(
                argv,
                timeout=max(1, timeout),
                max_output_bytes=self.max_output_bytes,
            )
            returncode = completed.returncode
            final_stdout = completed.stdout
            final_stderr = completed.stderr
            stdout_truncated = completed.stdout_truncated
            stderr_truncated = completed.stderr_truncated or completed.timed_out
            if completed.timed_out:
                message = f"execution timed out after {timeout} seconds\n".encode()
                final_stderr += message[: max(0, self.max_output_bytes - len(final_stderr))]
            stdout_path = logs_root / f"{index:02d}-{step.role}.stdout.log"
            stderr_path = logs_root / f"{index:02d}-{step.role}.stderr.log"
            stdout_path.write_bytes(final_stdout)
            stderr_path.write_bytes(final_stderr)
            log_refs.extend([f"logs/{stdout_path.name}", f"logs/{stderr_path.name}"])
            if returncode != 0:
                break
        outputs = [
            path
            for path in bundle.expected_outputs
            if (area / Path(path)).is_file() and not (area / Path(path)).is_symlink()
        ]
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
            "meta": {"mechanism": "bubblewrap", "network_isolated": not self.network},
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
        area = self._validate_materialization(bundle=bundle, manifest=manifest, grant=grant)
        task = asyncio.current_task()
        if task is not None:
            self._active_executions.add(task)
        try:
            return await self._run(bundle=bundle, area=area, timeout=timeout)
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


class BubblewrapExecutionResourceProvider:
    name = "BubblewrapExecutionResourceProvider"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    provider_id = "bubblewrap"
    supported_kinds = ("code_execution",)
    _allowed_config = {"dependency_policy", "network"}

    @staticmethod
    def _on_register() -> None:
        return None

    @staticmethod
    def _on_unregister() -> None:
        return None

    def _tool_facts(self) -> dict[str, dict[str, Any]]:
        commands = {
            "python": ("python3", ("--version",)),
            "nodejs": ("node", ("--version",)),
            "go": ("go", ("version",)),
            "cpp": ("c++", ("--version",)),
        }
        facts: dict[str, dict[str, Any]] = {}
        for language, (tool, command_args) in commands.items():
            binary = shutil.which(tool)
            fact: dict[str, Any] = {
                "tool": {"nodejs": "node", "cpp": "c++"}.get(language, language),
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
                    raw = str(completed.stdout or completed.stderr).strip()[:300]
                    fact.update(
                        available=completed.returncode == 0,
                        raw_version=raw,
                        version=extract_code_toolchain_version(raw),
                    )
                except (OSError, subprocess.TimeoutExpired) as error:
                    fact.update(available=False, error=str(error)[:300])
            facts[language] = fact
        return facts

    @staticmethod
    def _validate_config(config: dict[str, Any]) -> None:
        unknown = sorted(set(config).difference(BubblewrapExecutionResourceProvider._allowed_config))
        if unknown:
            from agently.core import ExecutionResourceError

            raise ExecutionResourceError(
                "Bubblewrap provider configuration contains unsupported namespace or mount fields.",
                code="execution_resource.bubblewrap_config_invalid",
                payload={"unsupported_fields": unknown},
            )

    def create_resource(
        self,
        *,
        grant: TaskWorkspaceAccessGrant,
        max_output_bytes: int,
        network: bool,
    ) -> BubblewrapCodeExecutionResource:
        return BubblewrapCodeExecutionResource(
            grant=grant,
            max_output_bytes=max_output_bytes,
            network=network,
        )

    async def async_probe(
        self,
        *,
        requirement: "ExecutionResourceRequirement",
        policy: "ExecutionResourcePolicy",
    ) -> "ExecutionResourceProviderProbe":
        _ = requirement, policy
        availability = await asyncio.to_thread(inspect_bubblewrap_availability)
        available = bool(availability.get("available"))
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
                "safety_class": "namespace",
            },
            "reason": "ready" if available and languages else str(availability.get("reason", "toolchain_unavailable")),
            "meta": {"availability": availability, "toolchains": facts},
        }

    async def async_ensure(
        self,
        *,
        requirement: "ExecutionResourceRequirement",
        policy: "ExecutionResourcePolicy",
        existing_handle: "ExecutionResourceHandle | None" = None,
    ) -> "ExecutionResourceHandle":
        _ = existing_handle
        from agently.core import ExecutionResourceError

        config = requirement.get("config", {})
        config = dict(config) if isinstance(config, dict) else {}
        self._validate_config(config)
        grant = requirement.get("task_workspace_access_grant")
        if not isinstance(grant, TaskWorkspaceAccessGrant):
            raise ExecutionResourceError(
                "Bubblewrap code execution requires a TaskWorkspace access grant.",
                code="execution_resource.workspace_grant_required",
                payload={"provider_id": self.provider_id},
            )
        availability = await asyncio.to_thread(inspect_bubblewrap_availability)
        if not availability.get("available"):
            raise ExecutionResourceError(
                f"Bubblewrap is unavailable: {availability.get('reason', 'unknown')}",
                code="execution_resource.bubblewrap_unavailable",
                payload={"provider_id": self.provider_id, "availability": availability},
            )
        resource = self.create_resource(
            grant=grant,
            max_output_bytes=int(policy.get("max_output_bytes", 20000)),
            network=bool(config.get("network", False)),
        )
        try:
            verified = await asyncio.to_thread(inspect_bubblewrap_availability)
            if not verified.get("available"):
                raise ExecutionResourceError(
                    "Bubblewrap mechanism verification failed before handle readiness.",
                    code="execution_resource.bubblewrap_unavailable",
                    payload={"provider_id": self.provider_id, "availability": verified},
                )
        except BaseException:
            await resource.async_close()
            raise
        return {
            "handle_id": f"bubblewrap:{uuid.uuid4().hex}",
            "provider_id": self.provider_id,
            "resource": resource,
            "status": "ready",
            "meta": {
                "provider": self.name,
                "mechanism_verified": True,
                "availability": verified,
                "grant_id": grant.grant_id,
            },
        }

    async def async_health_check(
        self,
        handle: "ExecutionResourceHandle",
    ) -> "ExecutionResourceStatus":
        resource = handle.get("resource")
        meta = handle.get("meta")
        if (
            not isinstance(resource, BubblewrapCodeExecutionResource)
            or not isinstance(meta, dict)
            or not meta.get("mechanism_verified")
            or resource._closed
        ):
            return "unhealthy"
        availability = await asyncio.to_thread(inspect_bubblewrap_availability)
        return "ready" if availability.get("available") else "unhealthy"

    async def async_release(self, handle: "ExecutionResourceHandle") -> None:
        resource = handle.get("resource")
        if isinstance(resource, BubblewrapCodeExecutionResource):
            await resource.async_close()


__all__ = [
    "BubblewrapCodeExecutionResource",
    "BubblewrapExecutionResourceProvider",
    "inspect_bubblewrap_availability",
]
