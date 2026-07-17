# Seatbelt Sandbox Provider

> **Platform**: macOS only  
> **Language**: [English](../../en/seatbelt/README.md) · **中文**

---

## 概述

Seatbelt 是 macOS 自带的内核级沙箱机制，通过 `sandbox-exec` 工具和 SBPL (Seatbelt Profile Language) 实现系统调用过滤。

本 Provider 将 Seatbelt 集成为 Agently 的 `ExecutionResourceProvider`，为代码执行提供轻量级、无需 Docker 的隔离环境。

---

## 核心特性

| 特性 | 说明 |
|------|------|
| **零依赖** | 使用 macOS 自带工具，无需安装 Docker |
| **内核级隔离** | 通过 SBPL 实现 syscall 过滤 |
| **灵活策略** | 支持白名单写入、受保护路径、禁止读取路径 |
| **last-match-wins** | SBPL 规则按顺序匹配，后定义的规则覆盖前面的 |

---

## SBPL Profile 设计

Profile 遵循以下设计原则（借鉴 OpenHanako）：

```sbpl
(version 1)
(deny default)

;; ═══ 基础能力（始终放行）═══
(allow process-exec* process-fork signal)
(allow sysctl-read)
(allow mach(*))
(allow ipc-posix*)

;; ═══ 文件读取：全局允许 ═══
;; AI agent 需要读系统库才能执行命令
(allow file-read*)

;; ═══ 文件写入：白名单 ═══
(allow file-write* (subpath "/workspace"))
(allow file-write* (subpath "/private/tmp"))
(allow file-write* (subpath "$TMPDIR"))

;; ═══ 设备文件 ═══
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/ptmx"))
(allow pseudo-tty)

;; ═══ 受保护路径（last-match-wins）═══
(deny file-write* (subpath "/workspace/.git"))

;; ═══ 禁止读取（保护 secrets）═══
(deny file-read* (subpath "/home/user/.env"))
(deny file-write* (subpath "/home/user/.env"))

;; ═══ 网络开关 ═══
(allow network-outbound)  ;; 或 (deny network)
```

---

## 配置参数

### `register_seatbelt_sandbox_action()` 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `action_id` | `str` | `"seatbelt_sandbox"` | Action 标识符 |
| `network` | `bool` | `False` | 是否允许网络出站 |
| `writable_paths` | `list[str]` | `[]` | 可写路径白名单 |
| `protected_paths` | `list[str]` | `[]` | 受保护路径（禁止写入，覆盖 writable_paths） |
| `deny_read_paths` | `list[str]` | `[]` | 禁止读取路径（同时禁止读写） |
| `extra_sbpl_rules` | `str` | `""` | 额外的 SBPL 规则（原样追加） |
| `timeout` | `int` | `60` | 执行超时（秒） |

---

## 使用示例

### 基础用法

```python
import agently

agent = agently.create_agent()

# 注册 Seatbelt 沙箱 action
agent.register_seatbelt_sandbox_action(
    action_id="safe_python",
    writable_paths=["/tmp/workspace"],
    protected_paths=["/tmp/workspace/.git"],
    network=False,
)

# 使用沙箱执行代码
result = agent.safe_python({
    "code": "print('Hello from Seatbelt sandbox!')"
})
```

### 保护敏感文件

```python
agent.register_seatbelt_sandbox_action(
    action_id="restricted_sandbox",
    writable_paths=["/workspace"],
    protected_paths=["/workspace/.git", "/workspace/config"],
    deny_read_paths=["/home/user/.env", "/home/user/.ssh"],
    network=False,
)
```

### 允许网络访问

```python
agent.register_seatbelt_sandbox_action(
    action_id="network_sandbox",
    writable_paths=["/workspace"],
    network=True,  # 允许 HTTP 请求
)
```

### 自定义 SBPL 规则

```python
agent.register_seatbelt_sandbox_action(
    action_id="custom_sandbox",
    writable_paths=["/workspace"],
    extra_sbpl_rules="""
;; 禁止访问特定 Mach 服务
(deny mach-lookup (global-name "com.apple.security.cryptd"))
;; 限制进程信息访问
(deny process-info*)
""",
)
```

---

## 路径安全

### realpath 防绕过

所有路径在写入 SBPL 前都会经过 `realpath()` 解析，防止通过符号链接绕过 `subpath` 限制：

```python
# 如果 /tmp/link -> /etc，则：
# (subpath "/tmp/link") 会被解析为 (subpath "/etc")
# 防止攻击者通过符号链接访问受保护路径
```

---

## 可用性检测

```python
from agently.builtins.plugins.ExecutionResourceProvider.SeatbeltExecutionResourceProvider import (
    inspect_seatbelt_availability,
    is_macos,
)

# 检查是否在 macOS
print(is_macos())  # True/False

# 检查 Seatbelt 可用性
result = inspect_seatbelt_availability()
print(result)
# {'available': True, 'binary': '/usr/bin/sandbox-exec', 'platform': 'macos'}
```

---

## 条件加载

Provider 仅在 macOS 上加载（通过 `__init__.py` 条件导入）：

```python
# agently/builtins/plugins/ExecutionResourceProvider/__init__.py
import platform

if platform.system() == "Darwin":
    from .SeatbeltExecutionResourceProvider import SeatbeltExecutionResourceProvider
```

非 macOS 平台不会加载此模块，零开销。

---

## 与 Docker Provider 对比

| 维度 | Seatbelt | Docker |
|------|----------|--------|
| **平台** | macOS only | 跨平台 |
| **依赖** | 无（系统自带） | 需要 Docker |
| **隔离级别** | syscall 过滤 | namespace + cgroup |
| **启动速度** | 极快（无容器） | 较慢（需启动容器） |
| **文件系统** | 共享宿主 | 独立文件系统 |
| **网络隔离** | 可选开关 | 完全隔离 |
| **适用场景** | macOS 开发环境 | 生产/CI 环境 |

---

## 限制

1. **仅 macOS**：非 macOS 平台无法使用
2. **文件读取全局允许**：AI agent 需要读系统库，无法完全限制读取
3. **网络无域名白名单**：只有全局开关，无法按域名过滤
4. **无 IPC 限制**：当前未限制跨进程通信

---

## 相关文档

- [ExecutionResource 概览](../actions/execution-environment.md)
- [多平台沙箱扩展框架](../sandbox-extension-framework.md)

---

> 语言：[English](../../en/seatbelt/README.md) · **中文**
