# 统一长任务 Loop

适用于 4.1.4.9 开发线。新目标驱动任务使用同一个长任务 Loop：模型根据原任务、有效上下文、可修改清单及真实操作结果，决定下一步或结束。TaskBoard 是 Markdown 清单，可增删、拆分、合并、重排、勾选和重新打开；它不编译成 DAG，也不强制逐卡执行或评估。

```python
execution = (
    agent.create_execution("long_task")
    .input("读取当前报名资料，汇总确认人员、所需资料和待确认事项。")
    .use_actions("read_document")
    .output(str)
)
result = await execution.async_get_data()
```

ContextPackage 继续供应任务相关资料。工具由既有 ActionRuntime 执行，下一轮读取实际结果；独立操作可以同批执行，依赖新结果的操作必须等待下一轮。不默认增加目标补全、逐卡审阅或终态 judge 请求；调用方显式配置的 validate/artifact/review 政策仍按原合同执行。

结果正文或结构化对象通过 async_get_data() 读取，类型化对象使用 async_get_data_object()。async_get_full_data() 同时保留 status、accepted、taskboard 和 final_result。模型判断无法继续时返回 blocked，并保留有用成果，说明未完成事项、需要检查的不确定性、实际风险和所需补充信息；这不是准确率保证。

Host 兑现明确承诺：require_actions() 要求指定 Action 真正成功；task_options 的 options.agent_task.required_deliverables 可声明必须存在于指定位置的文件。目标文件不能用聊天正文或 fallback 路径冒充；失败事实回到同一个 Loop。文件正文语义仍按原任务判断，不额外发起独立评估。需要 Host 将最终结果保存成文件时，沿用 artifact(path) 政策。

清单进度通过 long_task.progress 事件观察（taskboard、status、round），模型阶段沿用 execution.stage.*，工具沿用 Action 日志。round 是 Host 计数，不注入模型预算要求。max_iterations 默认 20；模型调用和时间限制仍由 execution limits 管理，返工后累计。

async_pause() 在下一决策前的已结算边界暂停，包括操作批次结束后；没有已结算观察的活跃操作不能保存。捕获 AgentExecutionPaused 后 save()，在相同配置的新 execution 上 load()、async_resume()，不会重放已完成操作。async_rework() 保留原任务、清单和实际观察，在同一 Loop 处理反馈；重放权限仍由 Host 控制。async_interrupt() 或兼容 async_add_guidance() 的补充信息在后续 ContextPackage 中消费。取消等待已拥有工作清理完成。

## 迁移

4.1.x 中显式 strategy("flat") / strategy("taskboard")、create_task(execution=...) 和旧 RecordStore task_id 恢复保留原实现。旧快照继续在旧路径恢复，不自动转换为新 Loop 或重放操作；新任务直接选 long_task，不再为任务复杂度选择 Flat/TaskBoard。旧卡片、验证和调度选项仅用于上述兼容路径。

4.2 使用 create_execution("long_task") 和 execution 的 save/load/resume；移除旧策略名、Agent.create_task/create_task_loop、按旧 task_id 的 Agent.resume 别名。旧状态应在 4.1.x 完成或导出任务成果后开始新任务，不声称无损转换。独立 TaskDAG 和 TriggerFlow 的用途保持不变。
