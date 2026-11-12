# DevTools 0.2 评审闭环

本功能需要 `agently-devtools >=0.2.0,<0.3.0`；0.1.x 不提供评审接口。
Agently RuntimeEvent 协议不变，应用仍可不依赖 DevTools 运行。

在运行详情中查看实际输入、Prompt、输出和交接，再添加评审意见。意见可绑定
整个运行或该节点的事件，并注明阶段验收标准与“观察／假设／建议”的性质。
Coding Agent 按 Agently-Skills 指引读取同一份记录和原始证据，在用户已授权的
范围内修改，再将复验运行关联到原意见。评审者检查后决定接受或重新处理。

`addressed` 表示已回应、待评审；`accepted` 是评审记录状态，不代表运行结果
被修改或代码操作获准。署名是本机协作标记，不是认证身份。原始证据过期时
会明确显示不可用，历史意见不会替代事实。

```python
# 读取评审服务的意见；无需新增 framework 入口。
import httpx
with httpx.Client(base_url=base_url, headers=headers) as client:
    page = client.get('/reviews', params={
        'app_id': app_id, 'group_id': group_id, 'status': 'open',
        'limit': 50, 'offset': 0,
    }).raise_for_status().json()['data']
    # 根据 next_offset 翻页；详情 /reviews/{review_id} 包含完整回复历史。
```

写入采用客户端 operation_id 幂等和 revision 并发检查。复验应保持用例、
质量标准和模型配置可比；独立记录输入变更、代码版本、调用次数、用量、耗时
和局限。界面与 HTTP API 共享记录，均不会自动执行修改、重试或合并分支。

DevTools 0.2把缺失的 provider token 计数显示为 `unavailable`，字符长度估算仍单独标示，不作为 token 或费用。

## 完整动线（协议3）

从“评审待办”按应用/分组/状态找到意见，再按“现状证据、修改与决定、实验结果、
回复与历史”核对同一对象。现状显示保留的实际Prompt及输出，节点提案按职责、
交接及slot当前/提议内容呈现，实验关联现有EvaluationRunner suite。
先声明固定用例、版本候选及节点/端到端标准；没有实验记录不等于通过。

“预览 Agent 接手说明”生成可复制说明。Agent按Agently-Skills读取原证据并核对源码，
实施已授权修改，再回传plan_revision、应用源版本、实际请求事件和复验运行。
源版本是作者声明，DevTools不会自动读本地源码或证明语义一致；评审者需核对
提案、源码、实际派发与效果。修订提案重新open，旧版本落地回传会被拒绝。

草稿在当前浏览器标签页内跨跳转/刷新保留；冲突后读取新版历史，再明确采用新版，
不自动把旧稿移到新版。诊断控件可收纳；全局重连刷新意见，错误就地指明引用问题。
评审接口不执行代码、不授予授权。

## 全站工作区与明确决定（协议3）

全局导航按运行观测、方案评测、审阅与修改、Playground、日志及支持入口组织。
应用/分组/时间是查询范围，具体运行/评审是当前对象；跨页、返回与刷新保留URL
定位。Interactive为独立试用页，不混入观测后台；表单与JSON切换保留实际字段。

评审plan的changes分别定位流程、Prompt或结果期望，记录改前/改后、依据、风险、
验证和采用/待定/暂缓。execution_mode区分核对、比较和实施；只有明确采用项进入
实施交接，依赖项需一并选定。保存决定与预览说明是两步；历史实验不续领调用预算。
回传application需绑定当前plan_revision及精确change_ids，另附真实派发事件和
复验run_ids。旧提案不自动采用。DevTools不证明源版本声明或模型语义正确性。
