# Agently 4.1.4.11 发布说明

Agently 4.1.4.11 仍支持 Python 3.10。Python 3.10 已于 2026 年 10 月 1 日结束
[官方维护](https://devguide.python.org/versions/#unsupported-versions)。
从 Agently 4.2 起，最低要求为 Python 3.11。Agently 4.2 仍在开发中。

| 范围 | 变更 | 建议用法 | 兼容性 | 验证 |
|---|---|---|---|---|
| 启动通知 | 在 Python 3.10 下，每个进程首次导入 Agently 时向标准错误输出一条提示。 | 升级到 Python 3.11 或更新版本，也可在导入前设置 `AGENTLY_PYTHON_310_NOTICE=0`。 | 4.1.4.11 仍支持 Python 3.10。 | 独立进程的启动和关闭通知测试。 |
| 状态复制 | 跳过对确切的内置不可变值的重复复制。 | 现有调用自动受益。 | 保留可变值和子类的复制行为。 | StateData 与 Agent 执行回归测试。 |
| 数据清理 | 加速普通嵌套容器的遍历。 | 现有调用自动受益。 | 保留键转换、类型选项和只读映射支持。 | DataFormatter、上下文投影与任务板证据测试。 |
| 原生工具调用 | 向 Chat Completions 发送 Action 定义，并读取流式和非流式工具调用。 | 配合支持工具调用的模型使用 `planning_protocol="native_tool_calls"`。 | 显式配置的同名函数优先。 | 真实模型和请求回归测试。 |
| 任务恢复 | 分别恢复任务板内部步数与任务迭代次数。 | 启用记录存储恢复后沿用现有恢复方法。 | 仍可读取旧快照。 | 任务板检查点与结果恢复测试。 |
| 检索重排 | 可选用 `model_type="rerank"` 和 `RerankProvider` 接入专用重排模型。 | 为 RecordStore 绑定重排提供方。 | 默认检索和重排行为保持原样。 | 真实重排模型与 RecordStore 测试。 |
| 配置文档与测试 | 补充 MiniMax Messages 地址说明，扩充 Silero 流状态和取消测试。 | 沿用现有 MiniMax 配置。 | Silero 运行时行为保持原样。 | 供应商请求和音频回归测试。 |
| Agently 4.2 | 更高的 Python 最低要求在 4.2 生效。 | 仍需要 Python 3.10 时可继续使用 4.1。 | 延后到 4.2。 | 包兼容性清单。 |

要关闭某个脚本的提示，在导入 Agently 前设置环境变量：

```bash
AGENTLY_PYTHON_310_NOTICE=0 python your_script.py
```

在 PowerShell 中，先运行 `$env:AGENTLY_PYTHON_310_NOTICE = "0"`，再运行脚本。
该设置只控制这条提示。

感谢 Dex Hünter（@dexhunter）贡献了 [StateData 复制优化](https://github.com/AgentEra/Agently/pull/370)
和[数据清理优化](https://github.com/AgentEra/Agently/pull/371)。
