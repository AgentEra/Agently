# Bubblewrap ExecutionResourceProvider

> Branch: `feature/bubblewrap-provider`
> Based on: Agently main (`1317da67`)
> Changes: 2 files, +530 lines

---

## Overview

This branch adds `BubblewrapExecutionResourceProvider`, using Linux `bwrap` (bubblewrap) to provide user namespace sandbox isolation.

Bubblewrap is the sandbox tool used by Flatpak, available in most Linux distributions, providing lightweight process isolation.

---

## Architecture

### Conforms to 4.1.4.2 Contract

```
provider_id = "bubblewrap"
supported_kinds = ("code_execution",)
```

Implements all required interfaces:
- `async_probe` — Detect bwrap availability
- `async_ensure` — Create execution resource
- `async_health_check` — Health check
- `async_release` — Release resource
- `async_execute_code` — Execute code (bundle/manifest/grant validation pattern)

### Conditional Loading

Only loaded on Linux systems:

```python
# __init__.py
if platform.system() == "Linux":
    from .BubblewrapExecutionResourceProvider import BubblewrapExecutionResourceProvider
```

---

## Configuration

### Via `code_execution.providers`

```python
settings.set("code_execution.providers", [
    {"provider_id": "bubblewrap", "config": {
        "unshare_all": True,
        "share_net": False,
        "writable_paths": ["/tmp/work"],
    }},
    {"provider_id": "docker", "config": {}},  # fallback
])
```

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `unshare_all` | bool | `True` | Isolate all namespaces |
| `share_net` | bool | `False` | Share network namespace |
| `writable_paths` | list[str] | `[]` | Writable path list |
| `readonly_paths` | list[str] | `[]` | Read-only path list |
| `tmpfs_paths` | list[str] | `[]` | tmpfs mount paths |
| `bwrap_binary` | str | `"bwrap"` | bwrap executable path |

---

## Isolation Capabilities

### Restriction Scope

| Capability | Support | Description |
|------------|---------|-------------|
| Process isolation | ✅ | PID namespace, sandbox processes are isolated |
| Filesystem isolation | ✅ | mount namespace + bind mounts |
| Network isolation | ✅ | network namespace, configurable sharing |
| User isolation | ✅ | user namespace, UID mapping |
| IPC isolation | ✅ | IPC namespace |
| UTS isolation | ✅ | UTS namespace (hostname) |
| cgroup isolation | ✅ | cgroup namespace |
| Syscall restriction | ❌ | bwrap doesn't use seccomp, requires extra config |
| Privilege escalation blocked | ✅ | user namespace prevents escalation |

### Configuration Parameters

```python
BubblewrapCodeExecutionResource(
    # === Filesystem Restrictions ===
    bind_ro=["/usr/share/data"],      # Read-only bind mounts
    bind_rw=["/var/work"],            # Read-write bind mounts
    tmpfs=["/tmp", "/run"],           # tmpfs mount points
    
    # === Namespace Isolation ===
    unshare_all=True,                 # Isolate all namespaces (recommended)
    share_net=False,                  # Share host network (isolated by default)
    
    # === Process Control ===
    clearenv=False,                   # Clear all environment variables
    new_session=True,                 # Create new session (block SIGHUP propagation)
    die_with_parent=True,             # Terminate sandbox when parent exits
    
    # === Advanced Options ===
    extra_bwrap_args=["--size", "1G"], # Additional bwrap arguments
)
```

### Default Read-only Bindings

The following system directories are read-only bound to the sandbox by default (if they exist):
- `/usr` — User programs
- `/bin` — Basic commands
- `/sbin` — System commands
- `/lib`, `/lib64` — Shared libraries
- `/etc/alternatives` — Alternative links

### Capabilities Returned by `async_probe`

```python
{
    "capabilities": {
        "isolation": {
            "process_contained": True,           # Processes are contained
            "host_filesystem_restricted": True,  # Host filesystem is restricted
            "privilege_escalation_blocked": True, # Privilege escalation blocked
            "syscalls_restricted": False,        # Syscalls not restricted
            "mechanism": "bubblewrap",
            "network_mode": "configurable",      # Network is configurable
        },
        "workspace_access_modes": ["snapshot", "read_only", "read_write"],
        "network": "configurable",
        "safety_class": "isolated",
    }
}
```

---

## Comparison with Docker Provider

| Feature | Bubblewrap | Docker |
|---------|------------|--------|
| Startup speed | Fast (no daemon) | Slower (requires daemon) |
| Memory overhead | Low | Higher |
| Isolation strength | User namespace | Full container |
| Image support | ❌ | ✅ |
| Cross-platform | Linux only | Linux/macOS/Windows |

---

## System Requirements

- Linux kernel 3.8+ (user namespaces)
- `bwrap` installed (`apt install bubblewrap` or `dnf install bubblewrap`)
- Kernel parameter `kernel.unprivileged_userns_clone=1` (Debian/Ubuntu)

---

## File List

| File | Description |
|------|-------------|
| `BubblewrapExecutionResourceProvider.py` | Complete provider implementation (530 lines) |
| `__init__.py` | Linux conditional import |
