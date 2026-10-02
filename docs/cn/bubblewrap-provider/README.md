# Bubblewrap ExecutionResourceProvider

> 分支：`feature/bubblewrap-provider`
> 基于：Agently main (`1317da67`)
> 改动范围：2 个文件，+530 行

---

## 概述

本分支新增 `BubblewrapExecutionResourceProvider`，使用 Linux `bwrap` (bubblewrap) 提供用户命名空间沙箱隔离。

Bubblewrap 是 Flatpak 使用的沙箱工具，可在大多数 Linux 发行版中使用，提供轻量级的进程隔离。

---

## 架构设计

### 符合 4.1.4.2 契约

```
provider_id = "bubblewrap"
supported_kinds = ("code_execution",)
```

实现了所有必需接口：
- `async_probe` — 检测 bwrap 可用性
- `async_ensure` — 创建执行资源
- `async_health_check` — 健康检查
- `async_release` — 释放资源
- `async_execute_code` — 执行代码（bundle/manifest/grant 验证模式）

### 条件加载

仅在 Linux 系统加载：

```python
# __init__.py
if platform.system() == "Linux":
    from .BubblewrapExecutionResourceProvider import BubblewrapExecutionResourceProvider
```

---

## 配置方式

### 通过 `code_execution.providers` 配置

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

### 配置选项

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `unshare_all` | bool | `True` | 隔离所有命名空间 |
| `share_net` | bool | `False` | 是否共享网络命名空间 |
| `writable_paths` | list[str] | `[]` | 可写路径列表 |
| `readonly_paths` | list[str] | `[]` | 只读路径列表 |
| `tmpfs_paths` | list[str] | `[]` | tmpfs 挂载路径 |
| `bwrap_binary` | str | `"bwrap"` | bwrap 可执行文件路径 |

---

## 隔离能力

### 限制范围设置

| 能力 | 支持 | 说明 |
|------|------|------|
| 进程隔离 | ✅ | PID namespace，沙箱内进程独立 |
| 文件系统隔离 | ✅ | mount namespace + bind mounts |
| 网络隔离 | ✅ | network namespace，可配置共享 |
| 用户隔离 | ✅ | user namespace，UID 映射 |
| IPC 隔离 | ✅ | IPC namespace |
| UTS 隔离 | ✅ | UTS namespace（主机名） |
| cgroup 隔离 | ✅ | cgroup namespace |
| 系统调用限制 | ❌ | bwrap 不做 seccomp，需额外配置 |
| 权限提升阻止 | ✅ | user namespace 阻止提权 |

### 配置参数详解

```python
BubblewrapCodeExecutionResource(
    # === 文件系统限制 ===
    bind_ro=["/usr/share/data"],      # 只读绑定到沙箱
    bind_rw=["/var/work"],            # 读写绑定到沙箱
    tmpfs=["/tmp", "/run"],           # tmpfs 挂载点
    
    # === 命名空间隔离 ===
    unshare_all=True,                 # 隔离所有命名空间（推荐）
    share_net=False,                  # 共享宿主机网络（默认隔离）
    
    # === 进程控制 ===
    clearenv=False,                   # 清除所有环境变量
    new_session=True,                 # 创建新会话（阻止 SIGHUP 传播）
    die_with_parent=True,             # 父进程退出时终止沙箱
    
    # === 高级选项 ===
    extra_bwrap_args=["--size", "1G"], # 额外 bwrap 参数
)
```

### 默认只读绑定

以下系统目录默认只读绑定到沙箱（如存在）：
- `/usr` — 用户程序
- `/bin` — 基本命令
- `/sbin` — 系统命令
- `/lib`, `/lib64` — 共享库
- `/etc/alternatives` — 替代链接

### `async_probe` 返回的能力信息

```python
{
    "capabilities": {
        "isolation": {
            "process_contained": True,           # 进程被限制
            "host_filesystem_restricted": True,  # 宿主机文件系统受限
            "privilege_escalation_blocked": True, # 阻止权限提升
            "syscalls_restricted": False,        # 系统调用未限制
            "mechanism": "bubblewrap",
            "network_mode": "configurable",      # 网络可配置
        },
        "workspace_access_modes": ["snapshot", "read_only", "read_write"],
        "network": "configurable",
        "safety_class": "isolated",
    }
}
```

---

## 与 Docker Provider 对比

| 特性 | Bubblewrap | Docker |
|------|------------|--------|
| 启动速度 | 快（无需 daemon） | 较慢（需要 daemon） |
| 内存开销 | 低 | 较高 |
| 隔离强度 | 用户命名空间 | 完整容器 |
| 镜像支持 | ❌ | ✅ |
| 跨平台 | Linux only | Linux/macOS/Windows |

---

## 系统要求

- Linux 内核 3.8+ (user namespaces)
- `bwrap` 已安装 (`apt install bubblewrap` 或 `dnf install bubblewrap`)
- 内核参数 `kernel.unprivileged_userns_clone=1` (Debian/Ubuntu)

---

## 文件清单

| 文件 | 说明 |
|------|------|
| `BubblewrapExecutionResourceProvider.py` | 完整 provider 实现 (530 行) |
| `__init__.py` | Linux 条件导入 |
