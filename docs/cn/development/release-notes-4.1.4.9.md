---
title: Agently 4.1.4.9 发布说明
description: 统一长任务 Loop、有效上下文、独立模型能力、SystemOne 与 instant 流。
---

# Agently 4.1.4.9 发布说明

这是发布准备中的候选版本，尚未公开发布。升级范围以已发布的 4.1.4.8 为基线。
Python 要求保持 3.10+，运行时依赖 Agently-Stage >=0.3.8,<0.4.0；可选
DevTools 推荐 >=0.2.0,<0.3.0，Agently-Skills 使用对齐 4.1.4.9 的 V3 catalog。

## 推荐用法

以下代码使用已配置模型和已注册 `read_document` Action 的 Agent：

```python
execution = (
    agent.create_execution("long_task")
    .input("读取最新报名资料，汇总确认人数、物料和仍需补充的信息。")
    .use_actions("read_document")
    .output(str)
)
result = await execution.async_get_data()
```

新任务无需在 Flat 与 TaskBoard 之间选策略。模型在一个 Loop 中使用可修订的 Markdown
清单推进目标，ContextPackage 提供有效资料，ActionRuntime 返回真实操作结果。
指定交付文件可继续使用 `.artifact("reports/summary.md")` 由 Host 写入最终结果。

```python
# 4.1.x 旧入口保留，旧任务状态继续使用原执行路径。
legacy = agent.create_task(goal="原有任务", execution="taskboard")
# 新任务推荐入口；不是对旧状态的自动迁移。
current = agent.create_execution("long_task").input("新的任务")
```

## 核心变化

| 范围 | 变化 | 推荐用法 | 兼容 / 风险 | 验证入口 |
|---|---|---|---|---|
| 长任务 | 一个决策循环、可编辑清单、真实 Action 观察；普通新任务不再使用逐卡执行/评估链 | `create_execution("long_task")` | 4.1.x 保留旧策略和 task-id 恢复；4.2 才移除 | `examples/agent_task/unified_loop_roster.py`、`tests/test_long_task_loop.py` |
| 上下文 / Skills | 选择时提供完整任务事实，复用资料读取，减少冗余索引和 Host 预算投影 | 现有 Skills 与 ContextPackage 接口 | 权限、作用域和 Host 预算仍有效 | `examples/skills_executor/12_complete_task_selection.py`、Context/Skill 测试 |
| 模型能力 | LLM/VLM/OCR/embedding/STT/TTS 独立配置和按能力执行 | [模型能力](../models/model-capabilities-guide.md) | 不隐式继承其他角色的凭据和参数 | 模型能力示例与协议测试 |
| SystemOne / Jev | 输出模板选择独立模型，支持 Probability/Choice/Score 与字段依赖；LLM 阶段转发 instant | 配置 `system_one`，普通字段仍由普通模型处理 | 已选 provider 失败不暗中换模型；未配置时保持普通模型路径 | `tests/test_system_one.py`、`tests/test_judgment_output.py` |
| instant 与校验 | 仅实际 SystemOne 阶段在完整字段已观察后禁止重放 | 普通 instant 保留校验失败后的有界重试，以最终结果为准 | 普通 Agent、ModelRequest 及组合中的普通 LLM 阶段保持既有重试行为 | 冻结示例 06、validate 与 SystemOne 回归 |
| 语音输入 | 可选声学检测和有界 PCM/WAV 分段，保留原始采样时间轴 | 显式传入 `AudioInputOptions` | 默认行为不变；不保证语义去噪或任意格式解码 | 音频输入预处理示例与测试 |
| 安装后的类型提示 | 明确导出已有公开类型，使配套包正确识别 | 原导入路径不变 | 不改变运行时对象 | 安装包类型 smoke、DevTools 类型检查 |
| 延期 | 4.2 旧入口移除、活跃子任务/内部任意位置快照、无损旧状态转换 | 继续使用已声明的安全暂停点 | 本版不承诺这些能力 | [长任务迁移与边界](../start/long-task-loop.md) |

## instant 的升级注意事项

```python
response = agent.create_request().input("任务").output({"answer": str}).get_result()
async for item in response.get_async_generator(type="instant"):
    render_provisional(item)  # 应用自己的临时展示函数
# 普通请求仍可校验重试；以通过校验的最终结果更新临时展示。
final = await response.async_get_data(max_retries=2)
```

普通 ModelRequest 和 Agent 保留原有有界输出校验重试，不因读取 instant 改变。
只有实际 SystemOne 阶段在完整字段被观察后不重放；普通前后置阶段不受此限制。
provider 传输重试与 `$status` 的重放边界仍遵循 provider 契约；本变更针对输出校验重试。
不要从临时值直接执行不可撤销操作。详见 [模型与流集成](../triggerflow/model-integration.md)。

## 已知限制

模型自行判断完成程度不构成正确性保证。当前 Qwen 样本仍出现最终正确数字旁残留旧草稿、
合法负面结论被标为 blocked 等情况；应用应按自身风险选择显式 `validate` / `review`。
任务未完成时保留有用结果，并说明未满足内容、待核实项、风险和缺失信息；披露缺失不等于完成。
精确交付位置及必需 Action 仍由 Host 校验，不能以聊天文本或备用文件代替。
旧 TaskBoard 普通答案由原 finalizer 判定完成，显式交付/能力合同保留必要检查。
本版不以历史实验中的单次成功承诺通用准确率、延迟或必然收敛。
