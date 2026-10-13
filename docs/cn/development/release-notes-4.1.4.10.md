# Agently 4.1.4.10 候选说明

本次候选修复长任务的文件交付机制，保留 4.1 的现有接口与兼容入口。
4.2.0 尚未发布；此补丁不纳入 4.2 的兼容移除。

| 范围 | 变化 | 使用方式 | 兼容与边界 | 证据 |
|---|---|---|---|---|
| 文件观察与终态交付 | 原生 Action 文件引用进入 Loop，选中文件经身份核验后保留 | 在结构化结果中返回 Action 实际提供的完整路径 | 不从摘要提取路径；未选私有中间文件仍清理 | `tests/test_long_task_loop.py`、`examples/agent_task/unified_loop_file.py` |
| 隐式文件 Actions | 读写使用当前 execution 的工作区身份 | Agent 配置的原生文件 Actions 可供 execution 继承 | 显式 root/workspace 和 isolated 权限边界保持不变 | 原生读回代码制品及执行私有写入回归 |
| FastMCP 适配 | 读取 SDK v2 Schema 与标签，保留空结构化对象、错误与内容 wire 别名 | 可选安装 `fastmcp==4.1.0`，继续使用 `agent.use_mcp(...)` | FastMCP 3.x 保留；交互式工具和后台任务扩展未新增 | 双版本真实 MCP 协议回归、`2_5_mcp_protocol_compat.py` |
| deferred：模型效果 | 本次不增加默认语义 Judge 或未验证的 Prompt 策略 | 单独检查业务完成、来源一致性及内容/视觉质量 | 文件可交付不代表模型业务判断正确；部分报告不自动表示整体完成 | 长任务机制测试与真实效果评估分开记录 |

配置好模型和工作区后，继续使用现有调用形状：

```python
execution = (
    agent.create_execution("long_task")
    .input(task)
    .use_actions(["read_file", "write_file"])
    .output({"files": [str], "summary": str})
)
result = await execution.async_get_data()
```

`files` 中每个路径应独立填写 Action 已返回的完整路径。框架核对选中文件的
存在性、工作区、执行身份、大小和摘要，关闭后仍保留这些文件；文件变化或丢失
会反馈到同一个 Loop。`blocked` 也能保留已选的有用部分成果。

写入回执证明存储与身份，不证明正文已被审阅。需要内容检查时，应取得当前
实际正文，并按原任务与来源评估；需要不可绕过的验收时，使用已声明的
`validate`/review/交付政策，不能仅凭清单勾选或模型 `completed` 自评。
PDF 的原生字节发布仍需显式 artifact handler；默认 artifact 序列化业务结果。

先验证执行链路与执行逻辑，再检查当轮模型输入并做有界对照优化；未验证收益
不纳入修复。仍达不到声明的业务标准时，再经授权对比更强模型。不同层次的
通过结论不可互相替代。

这是候选变更说明。PyPI 发布、开发分支集成与正式发布验收分别核对。
