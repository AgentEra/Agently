<img width="640" alt="image" src="https://github.com/user-attachments/assets/c645d031-c8b0-4dba-a515-9d7a4b0a6881" />

# Agently 4 🚀

> **Build production‑grade AI apps faster, with stable outputs and maintainable workflows.**

[English Introduction](https://github.com/AgentEra/Agently/blob/main/README.md) | [中文介绍](https://github.com/AgentEra/Agently/blob/main/README_CN.md)

[![license](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/AgentEra/Agently/blob/main/LICENSE)
[![PyPI version](https://img.shields.io/pypi/v/agently.svg)](https://pypi.org/project/agently/)
[![Downloads](https://img.shields.io/pypi/dm/agently.svg)](https://pypistats.org/packages/agently)
[![GitHub Stars](https://img.shields.io/github/stars/AgentEra/Agently.svg?style=social)](https://github.com/AgentEra/Agently/stargazers)
[![Twitter Follow](https://img.shields.io/twitter/follow/AgentlyTech?style=social)](https://x.com/AgentlyTech)
<a href="https://doc.weixin.qq.com/forms/AIoA8gcHAFMAScAhgZQABIlW6tV3l7QQf">
<img alt="WeChat" src="https://img.shields.io/badge/WeChat%20Group-Join-brightgreen?logo=wechat&style=flat-square">
</a>

<p align="center">
  <a href="https://github.com/AgentEra/Agently/discussions"><img src="https://img.shields.io/badge/💬_Community-Join-blueviolet?style=for-the-badge"></a>
  <a href="https://agently.tech"><img src="https://img.shields.io/badge/🌐_Website-Docs-brightgreen?style=for-the-badge"></a>
  <a href="https://github.com/AgentEra/Agently/issues"><img src="https://img.shields.io/badge/🐛_Issues-Report-red?style=for-the-badge"></a>
</p>

---

<p align="center">
  <b>🔥 <a href="https://agently.tech/docs">Latest Docs</a> | 🚀 <a href="#quickstart">5‑minute Quickstart</a> | 💡 <a href="#-core-features">Core Features</a></b>
</p>

---

## 📚 Quick Links

- **Docs (EN)**: https://agently.tech/docs
- **Docs (中文)**: https://agently.cn/docs
- **Agent Systems Playbook (EN)**: https://agently.tech/docs/en/agent-systems/overview.html
- **Agent Systems Playbook (中文)**: https://agently.cn/docs/agent-systems/overview.html
- **Coding Agent Guide (EN)**: https://agently.tech/docs/en/agent-docs.html
- **Coding Agent Guide (中文)**: https://agently.cn/docs/agent-docs.html
- **Agent Docs Pack**: https://agently.cn/docs/agent_docs.zip

## 🤔 Why Agently?

Many GenAI POCs fail in production not because models are weak, but because **engineering control is missing**:

| Common challenge | How Agently helps |
|:--|:--|
| Output schema drifts, JSON parsing fails | **Contract‑first output control** with `output()` + `ensure_keys` |
| Workflows get complex and hard to maintain | **TriggerFlow orchestration** with `to` / `if` / `match` / `batch` / `for_each` |
| Multi‑turn state becomes unstable | **Session & Memo** with memory, summaries, and persistence strategies |
| Tool calls are hard to audit | **Tool logs** via `extra.tool_logs` |
| Switching models is expensive | **OpenAICompatible** unified model settings |

**Agently turns LLM uncertainty into a stable, testable, maintainable engineering system.**

## ✨ Core Features

### 1) 📝 Contract‑first Output Control
Define the structure with `output()`, enforce critical keys with `ensure_keys`.

```python
result = (
    agent
    .input("Analyze user feedback")
    .output({
        "sentiment": (str, "positive/neutral/negative"),
        "key_issues": [(str, "issue summary")],
        "priority": (int, "1-5, 5 is highest")
    })
    .start(ensure_keys=["sentiment", "key_issues[*]"])
)
```

### 2) ⚡ Structured Streaming (Instant)
Consume structured fields as they are generated.

```python
response = (
    agent
    .input("Explain recursion and give 2 tips")
    .output({"definition": (str, "one sentence"), "tips": [(str, "tip")]})
    .get_response()
)

for msg in response.get_generator(type="instant"):
    if msg.path == "definition" and msg.delta:
        ui.update_definition(msg.delta)
    if msg.wildcard_path == "tips[*]" and msg.delta:
        ui.add_tip(msg.delta)
```

### 3) 🧩 TriggerFlow Orchestration
Readable, testable workflows with branching and concurrency.

```python
(
    flow.to(handle_request)
    .if_condition(lambda d: d.value["type"] == "query")
    .to(handle_query)
    .elif_condition(lambda d: d.value["type"] == "order")
    .to(check_inventory)
    .to(create_order)
    .end_condition()
)
```

### 4) 🧠 Session & Memo (Multi‑turn Memory)
Quick / Lite / Memo modes with summaries and persistence strategies.

```python
from agently import Agently
from agently.core import Session

agent = Agently.create_agent()
session = Session(agent=agent).configure(
    mode="memo",
    limit={"chars": 6000, "messages": 12},
    every_n_turns=2,
)
agent.attach_session(session)
```

### 5) 🔧 Tool Calls + Logs
Tool selection and usage are logged in `extra.tool_logs`.

```python
@agent.tool_func
def add(a: int, b: int) -> int:
    return a + b

response = agent.input("12+34=?").use_tool(add).get_response()
full = response.get_data(type="all")
print(full["extra"]["tool_logs"])
```

### 6) 🌐 Unified Model Settings (OpenAICompatible)
One config for multiple providers and local models.

```python
from agently import Agently

Agently.set_settings(
    "OpenAICompatible",
    {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "auth": "DEEPSEEK_API_KEY",
    },
)
```

## 🚀 Quickstart

### Install

```bash
pip install -U agently
```

*Requirements: Python >= 3.10, recommended Agently >= 4.0.7.2*

### 5‑minute example

**1. Structured output**

```python
from agently import Agently

agent = Agently.create_agent()

result = (
    agent.input("Introduce Python in one sentence and list 2 advantages")
    .output({
        "introduction": (str, "one sentence"),
        "advantages": [(str, "advantage")]
    })
    .start(ensure_keys=["introduction", "advantages[*]"])
)

print(result)
```

**2. Workflow routing**

```python
from agently import TriggerFlow, TriggerFlowEventData

flow = TriggerFlow()

@flow.chunk
def classify_intent(data: TriggerFlowEventData):
    text = data.value
    if "price" in text:
        return "price_query"
    if "feature" in text:
        return "feature_query"
    if "buy" in text:
        return "purchase"
    return "other"

@flow.chunk
def handle_price(_: TriggerFlowEventData):
    return {"response": "Pricing depends on the plan..."}

@flow.chunk
def handle_feature(_: TriggerFlowEventData):
    return {"response": "Our product supports..."}

(
    flow.to(classify_intent)
    .match()
    .case("price_query")
    .to(handle_price)
    .case("feature_query")
    .to(handle_feature)
    .case_else()
    .to(lambda d: {"response": "What would you like to know?"})
    .end_match()
    .end()
)

print(flow.start("How much does it cost?"))
```

## ✅ Is Your App Production‑Ready? — Release Readiness Checklist

Based on teams shipping real projects with Agently, this **production readiness checklist** helps reduce common risks before release.

| Area | Check | Recommended Practice |
| :--- | :--- | :--- |
| **📝 Output Stability** | Are key interfaces stable? | Define schemas with `output()` and lock critical fields with `ensure_keys`. |
| **⚡ Real‑time UX** | Need updates while generating? | Consume `type="instant"` structured streaming events. |
| **🔍 Observability** | Tool calls auditable? | Inspect `extra.tool_logs` for full arguments and results. |
| **🧩 Workflow Robustness** | Complex flows fully tested? | Unit test each TriggerFlow branch and concurrency limit with expected outputs. |
| **🧠 Memory & Context** | Multi‑turn experience consistent? | Define Session/Memo summary, trimming, and persistence policies. |
| **📄 Prompt Management** | Can logic evolve safely? | Version and configure prompts to keep changes traceable. |
| **🌐 Model Strategy** | Can you switch or downgrade models? | Centralize settings with `OpenAICompatible` for fast provider switching. |
| **🚀 Performance & Scale** | Can it handle concurrency? | Validate async performance in real web‑service scenarios. |
| **🧪 Quality Assurance** | Regression tests complete? | Create fixed inputs with expected outputs for core scenarios. |


## 📈 Who Uses Agently to Solve Real Problems?

> "Agently helped us turn evaluation rules into executable workflows and keep key scoring accuracy at 75%+, significantly improving bid‑evaluation efficiency." — Project lead at a large energy SOE

> "Agently enabled a closed loop from clarification to query planning to rendering, reaching 90%+ first‑response accuracy and stable production performance." — Data lead at a large energy group

> "Agently’s orchestration and session capabilities let us ship a teaching assistant for course management and Q&A quickly, with continuous iteration." — Project lead at a university teaching‑assistant initiative

**Your project can be next.**  
📢 [Share your case on GitHub Discussions →](https://github.com/AgentEra/Agently/discussions/categories/show-and-tell)
## ❓ FAQ

**Q: How is Agently different from LangChain or LlamaIndex?**  
**A:** Agently is **built for production**. It focuses on stable interfaces (contract‑first outputs), readable/testable orchestration (TriggerFlow), and observable tool calls (`tool_logs`). It’s a better fit for teams that need reliability and maintainability after launch.

**Q: Which models are supported? Is switching expensive?**  
**A:** With `OpenAICompatible`, you can connect OpenAI, Claude, DeepSeek, Qwen and most OpenAI‑compatible endpoints, plus local models like Llama/Qwen. **The same business code can switch models without rewrites**, reducing vendor lock‑in.

**Q: What’s the learning curve? Where should I start?**  
**A:** The core API is straightforward—**you can run your first agent in minutes**. Start with [Quickstart](https://agently.tech/docs/en/quickstart.html), then dive into [Output Control](https://agently.tech/docs/en/output-control/overview.html) and [TriggerFlow](https://agently.tech/docs/en/triggerflow/overview.html).

**Q: How do I deploy an Agently‑based service?**  
**A:** Agently doesn’t lock you into a specific deployment path. It provides async APIs and FastAPI examples. The [FastAPI integration example](https://github.com/AgentEra/Agently/tree/main/examples/step_by_step/13-auto_loop_fastapi) covers SSE, WebSocket, and standard POST.

**Q: Do you offer enterprise support?**  
**A:** The core framework is open‑source under **Apache 2.0**. For enterprise support, training, or deep collaboration, contact us via the [community](https://doc.weixin.qq.com/forms/AIoA8gcHAFMAScAhgZQABIlW6tV3l7QQf).


## 🧭 Docs Guide (Key Paths)

- **Getting Started**
  - Quickstart: https://agently.tech/docs/en/quickstart.html
  - Model Settings: https://agently.tech/docs/en/model-settings.html
  - Coding Agent Guide: https://agently.tech/docs/en/agent-docs.html
- **Output Control (Structured Output)**
  - Overview: https://agently.tech/docs/en/output-control/overview.html
  - Output Format: https://agently.tech/docs/en/output-control/format.html
  - ensure_keys: https://agently.tech/docs/en/output-control/ensure-keys.html
  - Instant Streaming: https://agently.tech/docs/en/output-control/instant-streaming.html
- **Result & Streaming Events**
  - Result Data: https://agently.tech/docs/en/model-response/result-data.html
  - Streaming Events: https://agently.tech/docs/en/model-response/streaming.html
- **Session & Memo**
  - Overview: https://agently.tech/docs/en/agent-extensions/session-memo/
  - Quickstart: https://agently.tech/docs/en/agent-extensions/session-memo/quickstart.html
- **TriggerFlow Orchestration**
  - Overview: https://agently.tech/docs/en/triggerflow/overview.html
  - when Branch: https://agently.tech/docs/en/triggerflow/when-branch.html
  - if / elif / else: https://agently.tech/docs/en/triggerflow/if-branch.html
  - match / case: https://agently.tech/docs/en/triggerflow/match-branch.html
  - batch: https://agently.tech/docs/en/triggerflow/batch.html
  - for_each: https://agently.tech/docs/en/triggerflow/for-each.html
  - Runtime Stream: https://agently.tech/docs/en/triggerflow/runtime-stream.html
- **Tools & Extensions**
  - Tools: https://agently.tech/docs/en/agent-extensions/tools.html
  - MCP: https://agently.tech/docs/en/agent-extensions/mcp.html
  - auto_func: https://agently.tech/docs/en/agent-extensions/auto-func.html
  - KeyWaiter: https://agently.tech/docs/en/agent-extensions/key-waiter.html
- **Prompt Management**: https://agently.tech/docs/en/prompt-management/overview.html
- **Async & Settings**: https://agently.tech/docs/en/async-support.html / https://agently.tech/docs/en/settings.html
- **Playbook**: https://agently.tech/docs/en/agent-systems/overview.html

## 🤝 Community

- Discussions: https://github.com/AgentEra/Agently/discussions
- Issues: https://github.com/AgentEra/Agently/issues
- WeChat Group: https://doc.weixin.qq.com/forms/AIoA8gcHAFMAScAhgZQABIlW6tV3l7QQf

## 📄 License

Agently is licensed under [Apache 2.0](LICENSE).

---

<p align="center">
  <b>Start building your production‑ready AI apps →</b><br>
  <code>pip install -U agently</code>
</p>

<p align="center">
  <sub>Questions? Read the <a href="https://agently.tech/docs">docs</a> or join the <a href="https://doc.weixin.qq.com/forms/AIoA8gcHAFMAScAhgZQABIlW6tV3l7QQf">community</a>.</sub>
</p>

---

## Optional: verifiable receipts (proof-of-delivery)

Want a shareable proof link (preview + download) for an agent output?

Wrapper: https://github.com/jacobtop-tcg/claw2claw-receipt-wrapper
Demo: `examples/claw2claw_receipts_demo.py`

