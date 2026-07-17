# Seatbelt Sandbox Provider

> **Platform**: macOS only  
> **Language**: **English** · [中文](../../cn/seatbelt/README.md)

---

## Overview

Seatbelt is macOS's built-in kernel-level sandbox mechanism, implementing system call filtering through the `sandbox-exec` tool and SBPL (Seatbelt Profile Language).

This Provider integrates Seatbelt as an Agently `ExecutionResourceProvider`, providing lightweight, Docker-free isolation for code execution.

---

## Core Features

| Feature | Description |
|---------|-------------|
| **Zero Dependencies** | Uses macOS built-in tools, no Docker required |
| **Kernel-level Isolation** | Syscall filtering via SBPL |
| **Flexible Policies** | Supports whitelist writes, protected paths, deny-read paths |
| **last-match-wins** | SBPL rules match in order; later rules override earlier ones |

---

## SBPL Profile Design

The profile follows these design principles (inspired by OpenHanako):

```sbpl
(version 1)
(deny default)

;; ═══ Basic capabilities (always allowed) ═══
(allow process-exec* process-fork signal)
(allow sysctl-read)
(allow mach(*))
(allow ipc-posix*)

;; ═══ File read: globally allowed ═══
;; AI agents need to read system libs to execute commands
(allow file-read*)

;; ═══ File write: whitelist only ═══
(allow file-write* (subpath "/workspace"))
(allow file-write* (subpath "/private/tmp"))
(allow file-write* (subpath "$TMPDIR"))

;; ═══ Device files ═══
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/ptmx"))
(allow pseudo-tty)

;; ═══ Protected paths (last-match-wins) ═══
(deny file-write* (subpath "/workspace/.git"))

;; ═══ Deny-read (protect secrets) ═══
(deny file-read* (subpath "/home/user/.env"))
(deny file-write* (subpath "/home/user/.env"))

;; ═══ Network switch ═══
(allow network-outbound)  ;; or (deny network)
```

---

## Configuration Parameters

### `register_seatbelt_sandbox_action()` Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `action_id` | `str` | `"seatbelt_sandbox"` | Action identifier |
| `network` | `bool` | `False` | Allow network outbound access |
| `writable_paths` | `list[str]` | `[]` | Whitelist of writable paths |
| `protected_paths` | `list[str]` | `[]` | Protected paths (deny write, overrides writable_paths) |
| `deny_read_paths` | `list[str]` | `[]` | Deny-read paths (deny both read and write) |
| `extra_sbpl_rules` | `str` | `""` | Extra SBPL rules (appended verbatim) |
| `timeout` | `int` | `60` | Execution timeout (seconds) |

---

## Usage Examples

### Basic Usage

```python
import agently

agent = agently.create_agent()

# Register Seatbelt sandbox action
agent.register_seatbelt_sandbox_action(
    action_id="safe_python",
    writable_paths=["/tmp/workspace"],
    protected_paths=["/tmp/workspace/.git"],
    network=False,
)

# Execute code in sandbox
result = agent.Safe_python({
    "code": "print('Hello from Seatbelt sandbox!')"
})
```

### Protect Sensitive Files

```python
agent.register_seatbelt_sandbox_action(
    action_id="restricted_sandbox",
    writable_paths=["/workspace"],
    protected_paths=["/workspace/.git", "/workspace/config"],
    deny_read_paths=["/home/user/.env", "/home/user/.ssh"],
    network=False,
)
```

### Allow Network Access

```python
agent.register_seatbelt_sandbox_action(
    action_id="network_sandbox",
    writable_paths=["/workspace"],
    network=True,  # Allow HTTP requests
)
```

### Custom SBPL Rules

```python
agent.register_seatbelt_sandbox_action(
    action_id="custom_sandbox",
    writable_paths=["/workspace"],
    extra_sbpl_rules="""
;; Deny access to specific Mach services
(deny mach-lookup (global-name "com.apple.security.cryptd"))
;; Restrict process info access
(deny process-info*)
""",
)
```

---

## Path Security

### realpath Bypass Prevention

All paths are resolved via `realpath()` before being written to SBPL, preventing symlink-based bypass of `subpath` restrictions:

```python
# If /tmp/link -> /etc, then:
# (subpath "/tmp/link") is resolved to (subpath "/etc")
# Prevents attackers from accessing protected paths via symlinks
```

---

## Availability Detection

```python
from agently.builtins.plugins.ExecutionResourceProvider.SeatbeltExecutionResourceProvider import (
    inspect_seatbelt_availability,
    is_macos,
)

# Check if running on macOS
print(is_macos())  # True/False

# Check Seatbelt availability
result = inspect_seatbelt_availability()
print(result)
# {'available': True, 'binary': '/usr/bin/sandbox-exec', 'platform': 'macos'}
```

---

## Conditional Loading

The Provider is only loaded on macOS (via conditional import in `__init__.py`):

```python
# agently/builtins/plugins/ExecutionResourceProvider/__init__.py
import platform

if platform.system() == "Darwin":
    from .SeatbeltExecutionResourceProvider import SeatbeltExecutionResourceProvider
```

Non-macOS platforms will not load this module, resulting in zero overhead.

---

## Comparison with Docker Provider

| Dimension | Seatbelt | Docker |
|-----------|----------|--------|
| **Platform** | macOS only | Cross-platform |
| **Dependencies** | None (system built-in) | Requires Docker |
| **Isolation Level** | Syscall filtering | Namespace + cgroup |
| **Startup Speed** | Very fast (no container) | Slower (container startup) |
| **Filesystem** | Shared with host | Independent filesystem |
| **Network Isolation** | Optional switch | Full isolation |
| **Use Case** | macOS development | Production/CI environments |

---

## Limitations

1. **macOS Only**: Cannot be used on non-macOS platforms
2. **File Read Globally Allowed**: AI agents need to read system libs; cannot fully restrict reads
3. **No Domain-based Network Filtering**: Only global on/off switch, no per-domain filtering
4. **No IPC Restrictions**: Cross-process communication is not currently restricted

---

## Related Documentation

- [ExecutionResource Overview](../actions/execution-environment.md)
- [Multi-Platform Sandbox Extension Framework](../sandbox-extension-framework.md)

---

> Language: **English** · [中文](../../cn/seatbelt/README.md)
