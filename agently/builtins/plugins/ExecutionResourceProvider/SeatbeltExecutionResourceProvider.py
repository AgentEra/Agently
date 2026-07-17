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
SeatbeltExecutionResourceProvider — macOS Seatbelt sandbox backend.

Uses `sandbox-exec` with SBPL (Seatbelt Profile Language) to provide
kernel-level syscall filtering on macOS, as an alternative to Docker-based
isolation.

SBPL Profile Design (inspired by OpenHanako):
- Default deny all
- Basic capabilities always allowed (process, mach, ipc)
- File read: globally allowed (system libs needed for command execution)
- File write: whitelist only (writable_paths + temp dirs)
- Protected paths: deny write (after allow, last-match-wins)
- Deny-read paths: deny both read and write
- Device files: /dev/null, /dev/ptmx, pseudo-tty
- Network: optional outbound switch

This provider is **only** available on macOS. On other platforms it reports
itself as unavailable.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from agently.core.operation.ExecutionResource import (
    ExecutionResource,
    ExecutionResourceProvider,
)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def is_macos() -> bool:
    """Return True when running on macOS (Darwin)."""
    return platform.system() == "Darwin"


def inspect_seatbelt_availability() -> dict[str, Any]:
    """Check whether macOS Seatbelt (sandbox-exec) is usable.

    Returns a dict with at least ``{"available": bool}``. When unavailable
    the dict also contains a ``"reason"`` key.
    """
    if not is_macos():
        return {"available": False, "reason": "not_macos"}
    binary = shutil.which("sandbox-exec")
    if binary is None:
        return {"available": False, "reason": "sandbox_exec_missing"}
    return {
        "available": True,
        "binary": binary,
        "platform": "macos",
    }


# ---------------------------------------------------------------------------
# Path utilities
# ---------------------------------------------------------------------------

def _realpath(path: str) -> str:
    """Resolve symbolic links to prevent subpath bypass attacks.

    SBPL's (subpath "...") can be bypassed via symlinks if the target
    is outside the allowed subtree. Resolving to realpath prevents this.
    """
    try:
        return str(Path(path).resolve())
    except (OSError, ValueError):
        return path


def _get_tmpdir() -> str:
    """Get the actual temporary directory on macOS."""
    return os.environ.get("TMPDIR", "/tmp")


# ---------------------------------------------------------------------------
# SBPL profile generation
# ---------------------------------------------------------------------------

def _build_sbpl_profile(
    *,
    network: bool = False,
    writable_paths: list[str] | None = None,
    protected_paths: list[str] | None = None,
    deny_read_paths: list[str] | None = None,
    extra_rules: str = "",
) -> str:
    """Generate an SBPL (Seatbelt Profile Language) profile string.

    Design follows the "last-match-wins" rule: deny rules are placed AFTER
    allow rules to override them for specific paths.

    Parameters
    ----------
    network:
        Allow network outbound access when True.
    writable_paths:
        Filesystem paths where writing is allowed (whitelist).
    protected_paths:
        Paths where writing is explicitly denied (e.g., .git directories).
        These override writable_paths due to last-match-wins.
    deny_read_paths:
        Paths where both reading and writing are denied (e.g., secrets).
    extra_rules:
        Raw SBPL rule text appended verbatim at the end.
    """
    writable_paths = [_realpath(p) for p in (writable_paths or [])]
    protected_paths = [_realpath(p) for p in (protected_paths or [])]
    deny_read_paths = [_realpath(p) for p in (deny_read_paths or [])]
    tmpdir = _get_tmpdir()

    lines: list[str] = [
        "(version 1)",
        "(deny default)",
        "",
        ";; ═══ Basic capabilities (always allowed) ═══",
        "(allow process-exec* process-fork signal)",
        "(allow sysctl-read)",
        "(allow mach(*))",
        "(allow ipc-posix*)",
        "",
        ";; ═══ File read: globally allowed ═══",
        ";; AI agents need to read system libs to execute commands",
        "(allow file-read*)",
        "",
        ";; ═══ File write: whitelist only ═══",
    ]

    # Writable paths (user-specified workspace)
    for p in writable_paths:
        lines.append(f'(allow file-write* (subpath "{p}"))')

    # Temporary directories (always writable)
    lines.append("")
    lines.append(";; Temporary directories (always writable)")
    lines.append('(allow file-write* (subpath "/private/tmp"))')
    lines.append(f'(allow file-write* (subpath "{tmpdir}"))')

    # Device files
    lines.append("")
    lines.append(";; Device files + PTY")
    lines.append('(allow file-write* (literal "/dev/null"))')
    lines.append('(allow file-write* (regex #"^/dev/ttys[0-9]+$"))')
    lines.append('(allow file-write* (literal "/dev/ptmx"))')
    lines.append("(allow pseudo-tty)")

    # Protected paths: deny write (AFTER allow, last-match-wins)
    if protected_paths:
        lines.append("")
        lines.append(";; Protected paths: deny write (overrides allow)")
        for p in protected_paths:
            lines.append(f'(deny file-write* (subpath "{p}"))')

    # Deny-read paths: deny both read and write
    if deny_read_paths:
        lines.append("")
        lines.append(";; Deny-read paths: deny both read and write")
        for p in deny_read_paths:
            lines.append(f'(deny file-read* (subpath "{p}"))')
            lines.append(f'(deny file-write* (subpath "{p}"))')

    # Network switch
    lines.append("")
    if network:
        lines.append(";; Network: allow outbound")
        lines.append("(allow network-outbound)")
    else:
        lines.append(";; Network: completely denied")
        lines.append("(deny network)")

    # Extra rules (verbatim)
    if extra_rules.strip():
        lines.append("")
        lines.append(";; Extra rules")
        lines.append(extra_rules.strip())

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# SeatbeltExecutionResource
# ---------------------------------------------------------------------------

class SeatbeltExecutionResource(ExecutionResource):
    """Execute Python code inside a macOS Seatbelt sandbox.

    The sandbox profile follows these principles:
    - File read: globally allowed (needed for system libs)
    - File write: whitelist only (writable_paths + temp dirs)
    - Protected paths: deny write (overrides writable_paths)
    - Deny-read paths: deny both read and write (for secrets)
    - Network: optional outbound switch
    """

    def __init__(
        self,
        *,
        timeout: int = 60,
        network: bool = False,
        writable_paths: list[str] | None = None,
        protected_paths: list[str] | None = None,
        deny_read_paths: list[str] | None = None,
        extra_sbpl_rules: str = "",
        python_binary: str = "python3",
    ):
        self.timeout = timeout
        self.network = network
        self.writable_paths = writable_paths or []
        self.protected_paths = protected_paths or []
        self.deny_read_paths = deny_read_paths or []
        self.extra_sbpl_rules = extra_sbpl_rules
        self.python_binary = python_binary

    # -- availability -------------------------------------------------------

    def inspect_availability(self) -> dict[str, Any]:
        return inspect_seatbelt_availability()

    # -- SBPL profile -------------------------------------------------------

    def build_profile(self) -> str:
        """Generate the SBPL profile for this sandbox session."""
        return _build_sbpl_profile(
            network=self.network,
            writable_paths=self.writable_paths,
            protected_paths=self.protected_paths,
            deny_read_paths=self.deny_read_paths,
            extra_rules=self.extra_sbpl_rules,
        )

    # -- execution ----------------------------------------------------------

    async def run_python_code(
        self,
        *,
        python_code: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        effective_timeout = timeout or self.timeout
        profile_text = self.build_profile()

        cmd = [
            "sandbox-exec", "-f", "-",
            self.python_binary, "-c", python_code,
        ]

        try:
            result = subprocess.run(
                cmd,
                input=profile_text,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"Seatbelt sandbox timed out after {effective_timeout}s",
                "stdout": "",
                "stderr": "",
            }
        except FileNotFoundError:
            return {
                "ok": False,
                "error": "sandbox-exec binary not found (macOS only)",
                "stdout": "",
                "stderr": "",
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "stdout": "",
                "stderr": "",
            }

        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }


# ---------------------------------------------------------------------------
# SeatbeltExecutionResourceProvider
# ---------------------------------------------------------------------------

class SeatbeltExecutionResourceProvider(ExecutionResourceProvider):
    """Provider that creates Seatbelt-sandboxed execution resources.

    Registered with ``kind="seatbelt"``. Only functional on macOS.
    """

    @property
    def name(self) -> str:
        return "seatbelt"

    @property
    def kind(self) -> str:
        return "seatbelt"

    def inspect_availability(self) -> dict[str, Any]:
        return inspect_seatbelt_availability()

    def create_handle(
        self,
        *,
        config: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        availability = inspect_seatbelt_availability()
        if not availability.get("available"):
            return {
                "handle_id": f"seatbelt:{uuid.uuid4().hex}",
                "resource": None,
                "availability": availability,
                "meta": {
                    "provider": self.name,
                    "available": False,
                    "reason": availability.get("reason", "unknown"),
                },
            }

        resource = SeatbeltExecutionResource(
            timeout=int(policy.get("timeout_seconds", config.get("timeout", 60))),
            network=bool(config.get("network", False)),
            writable_paths=[str(p) for p in config.get("writable_paths", [])],
            protected_paths=[str(p) for p in config.get("protected_paths", [])],
            deny_read_paths=[str(p) for p in config.get("deny_read_paths", [])],
            extra_sbpl_rules=str(config.get("extra_sbpl_rules", "")),
            python_binary=str(config.get("python_binary", "python3")),
        )

        return {
            "handle_id": f"seatbelt:{uuid.uuid4().hex}",
            "resource": resource,
            "availability": availability,
            "meta": {
                "provider": self.name,
                "available": True,
                "platform": "macos",
                "network": resource.network,
            },
        }
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
SeatbeltExecutionResourceProvider — macOS Seatbelt sandbox backend.

Uses `sandbox-exec` with SBPL (Seatbelt Profile Language) to provide
kernel-level syscall filtering on macOS, as an alternative to Docker-based
isolation.

This provider is **only** available on macOS.  On other platforms it reports
itself as unavailable and the ExecutionResourceManager will refuse to create
handles for it.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import uuid
from typing import Any

from agently.core.operation.ExecutionResource import (
    ExecutionResource,
    ExecutionResourceProvider,
)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def is_macos() -> bool:
    """Return True when running on macOS (Darwin)."""
    return platform.system() == "Darwin"


def inspect_seatbelt_availability() -> dict[str, Any]:
    """Check whether macOS Seatbelt (sandbox-exec) is usable.

    Returns a dict with at least ``{"available": bool}``.  When unavailable
    the dict also contains a ``"reason"`` key.
    """
    if not is_macos():
        return {"available": False, "reason": "not_macos"}
    binary = shutil.which("sandbox-exec")
    if binary is None:
        return {"available": False, "reason": "sandbox_exec_missing"}
    return {
        "available": True,
        "binary": binary,
        "platform": "macos",
    }


# ---------------------------------------------------------------------------
# SBPL profile generation
# ---------------------------------------------------------------------------

def _build_sbpl_profile(
    *,
    network: bool = False,
    read_paths: list[str] | None = None,
    write_paths: list[str] | None = None,
    extra_rules: str = "",
) -> str:
    """Generate an SBPL (Seatbelt Profile Language) profile string.

    Parameters
    ----------
    network:
        Allow network access when *True*.
    read_paths:
        Extra filesystem paths allowed for reading.
    write_paths:
        Extra filesystem paths allowed for writing.
    extra_rules:
        Raw SBPL rule text appended verbatim.
    """
    read_paths = read_paths or []
    write_paths = write_paths or []

    lines: list[str] = [
        "(version 1)",
        "(deny default)",
        "",
        "# Always allow basic process operations",
        "(allow process-exec)",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "",
        "# Filesystem — deny by default, selectively allow",
        "(allow file-read* (subpath \"/usr/lib\")",
        "                 (subpath \"/usr/share\")",
        "                 (subpath \"/Library/Frameworks\")",
        "                 (subpath \"/System/Library/Frameworks\"))",
    ]

    for p in read_paths:
        lines.append(f'(allow file-read* (subpath "{p}"))')

    for p in write_paths:
        lines.append(f'(allow file-write* (subpath "{p}"))')

    if network:
        lines.extend([
            "",
            "# Network access",
            "(allow network*)",
        ])
    else:
        lines.extend([
            "",
            "# Network denied (default)",
            "(deny network*)",
        ])

    if extra_rules.strip():
        lines.append("")
        lines.append("# Extra rules")
        lines.append(extra_rules.strip())

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# SeatbeltExecutionResource
# ---------------------------------------------------------------------------

class SeatbeltExecutionResource(ExecutionResource):
    """Execute Python code inside a macOS Seatbelt sandbox."""

    def __init__(
        self,
        *,
        timeout: int = 60,
        network: bool = False,
        read_paths: list[str] | None = None,
        write_paths: list[str] | None = None,
        extra_sbpl_rules: str = "",
        python_binary: str = "python3",
    ):
        self.timeout = timeout
        self.network = network
        self.read_paths = read_paths or []
        self.write_paths = write_paths or []
        self.extra_sbpl_rules = extra_sbpl_rules
        self.python_binary = python_binary

    # -- availability -------------------------------------------------------

    def inspect_availability(self) -> dict[str, Any]:
        return inspect_seatbelt_availability()

    # -- SBPL profile -------------------------------------------------------

    def build_profile(self) -> str:
        return _build_sbpl_profile(
            network=self.network,
            read_paths=self.read_paths,
            write_paths=self.write_paths,
            extra_rules=self.extra_sbpl_rules,
        )

    # -- execution ----------------------------------------------------------

    async def run_python_code(
        self,
        *,
        python_code: str,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        effective_timeout = timeout or self.timeout
        profile_text = self.build_profile()

        cmd = [
            "sandbox-exec", "-f", "-",
            self.python_binary, "-c", python_code,
        ]

        try:
            result = subprocess.run(
                cmd,
                input=profile_text,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"Seatbelt sandbox timed out after {effective_timeout}s",
                "stdout": "",
                "stderr": "",
            }
        except FileNotFoundError:
            return {
                "ok": False,
                "error": "sandbox-exec binary not found (macOS only)",
                "stdout": "",
                "stderr": "",
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "stdout": "",
                "stderr": "",
            }

        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }


# ---------------------------------------------------------------------------
# SeatbeltExecutionResourceProvider
# ---------------------------------------------------------------------------

class SeatbeltExecutionResourceProvider(ExecutionResourceProvider):
    """Provider that creates Seatbelt-sandboxed execution resources.

    Registered with ``kind="seatbelt"``.  Only functional on macOS.
    """

    @property
    def name(self) -> str:
        return "seatbelt"

    @property
    def kind(self) -> str:
        return "seatbelt"

    def inspect_availability(self) -> dict[str, Any]:
        return inspect_seatbelt_availability()

    def create_handle(
        self,
        *,
        config: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        availability = inspect_seatbelt_availability()
        if not availability.get("available"):
            return {
                "handle_id": f"seatbelt:{uuid.uuid4().hex}",
                "resource": None,
                "availability": availability,
                "meta": {
                    "provider": self.name,
                    "available": False,
                    "reason": availability.get("reason", "unknown"),
                },
            }

        resource = SeatbeltExecutionResource(
            timeout=int(policy.get("timeout_seconds", config.get("timeout", 60))),
            network=bool(config.get("network", False)),
            read_paths=[str(p) for p in config.get("read_paths", [])],
            write_paths=[str(p) for p in config.get("write_paths", [])],
            extra_sbpl_rules=str(config.get("extra_sbpl_rules", "")),
            python_binary=str(config.get("python_binary", "python3")),
        )

        return {
            "handle_id": f"seatbelt:{uuid.uuid4().hex}",
            "resource": resource,
            "availability": availability,
            "meta": {
                "provider": self.name,
                "available": True,
                "platform": "macos",
                "network": resource.network,
            },
        }
