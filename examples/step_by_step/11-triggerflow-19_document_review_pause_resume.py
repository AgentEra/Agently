import asyncio
import os
from pprint import pprint
from typing import Any

from dotenv import find_dotenv, load_dotenv

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData


DOCUMENT_FOR_REVIEW = """
Service Agreement Draft

Clause 4. Payment:
The customer shall pay after acceptance. The acceptance date and acceptance
criteria will be confirmed later by both parties.

Clause 7. Termination:
The vendor may terminate the agreement immediately without prior notice if it
believes delivery conditions have changed.
"""


def configure_model() -> str:
    load_dotenv(find_dotenv())

    if os.getenv("DEEPSEEK_API_KEY"):
        Agently.set_settings(
            "OpenAICompatible",
            {
                "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                "model": os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash"),
                "model_type": "chat",
                "auth": os.getenv("DEEPSEEK_API_KEY"),
                "request_options": {
                    "thinking": {"type": "disabled"},
                    "temperature": 0.0,
                },
            },
        )
        provider = "deepseek"
    else:
        Agently.set_settings(
            "OpenAICompatible",
            {
                "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
                "api_key": os.getenv("OLLAMA_API_KEY", "ollama"),
                "model": os.getenv("OLLAMA_DEFAULT_MODEL", "qwen2.5:7b"),
                "model_type": "chat",
                "request_options": {"temperature": 0.0},
            },
        )
        provider = "ollama"

    Agently.set_settings("debug", False)
    return provider


async def ask_model_for_review_decision(document: str) -> dict[str, Any]:
    result = await (
        Agently.create_agent()
        .input(document)
        .instruct(
            [
                "You are the Plan step of a document review assistant.",
                "Decide whether the workflow should interrupt itself for human legal review.",
                "Use this policy:",
                "- unclear payment acceptance criteria is at least medium risk;",
                "- unilateral termination without prior notice is high risk;",
                "- any high risk issue must set should_pause to true.",
                "Return compact structured data only.",
            ]
        )
        .output(
            {
                "risk_level": ("'low' | 'medium' | 'high'", "overall legal risk level", True),
                "should_pause": (bool, "whether the model decides to interrupt for human review", True),
                "pause_reason": (str, "short reason shown to the reviewer", True),
                "issues": ([str], "key issues found in the document", True),
            }
        )
        .async_start(max_retries=1, raise_ensure_failure=False)
    )
    return result if isinstance(result, dict) else {}


def build_model_decided_pause_flow() -> TriggerFlow:
    flow = TriggerFlow(name="step-19-model-decided-document-review-pause")

    async def model_assess_document(data: TriggerFlowRuntimeData):
        document = str(data.input)
        decision = await ask_model_for_review_decision(document)
        await data.async_set_state("document", document, emit=False)
        await data.async_set_state("model_decision", decision, emit=False)
        return {
            "document": document,
            "decision": decision,
        }

    async def autonomous_pause_gate(data: TriggerFlowRuntimeData):
        if data.is_resume:
            assert data.resume.origin_signal is not None
            approval = data.resume.value if isinstance(data.resume.value, dict) else {}
            model_decision = data.get_state("model_decision") or {}
            status = "approved_after_human_review" if approval.get("approved") else "blocked_by_human_review"
            reviewed = {
                "status": status,
                "model_risk": model_decision.get("risk_level"),
                "approval": approval,
                "resume": {
                    "interrupt_id": data.resume.interrupt_id,
                    "origin_event": data.resume.origin_signal["trigger_event"],
                },
            }
            await data.async_set_state("reviewed", reviewed)
            return reviewed

        assessment = data.input if isinstance(data.input, dict) else {}
        raw_decision = assessment.get("decision")
        decision: dict[str, Any] = raw_decision if isinstance(raw_decision, dict) else {}

        if decision.get("should_pause"):
            return await data.async_pause_for(
                type="model_decided_legal_review",
                payload={
                    "risk_level": decision.get("risk_level"),
                    "reason": decision.get("pause_reason"),
                    "issues": decision.get("issues", []),
                },
                interrupt_id="model-decided-legal-review",
                resume_to="self",
            )

        reviewed = {
            "status": "auto_approved",
            "model_risk": decision.get("risk_level"),
            "approval": None,
        }
        await data.async_set_state("reviewed", reviewed)
        return reviewed

    async def finalize(data: TriggerFlowRuntimeData):
        reviewed = data.input if isinstance(data.input, dict) else {}
        final_report = {
            "status": reviewed.get("status"),
            "risk": reviewed.get("model_risk"),
            "human_approval_required": reviewed.get("approval") is not None,
        }
        await data.async_set_state("final_report", final_report)

    flow.to(model_assess_document).to(autonomous_pause_gate).to(finalize)
    return flow


async def main():
    provider = configure_model()
    print("[MODEL_PROVIDER]")
    print(provider)

    flow = build_model_decided_pause_flow()
    execution = flow.create_execution(auto_close=False)

    await execution.async_start(DOCUMENT_FOR_REVIEW)
    model_decision = execution.get_state("model_decision")
    pending = execution.get_pending_interrupts()

    print("[MODEL_DECISION]")
    pprint(model_decision)
    print("[PENDING_INTERRUPT]")
    pprint(pending)

    assert model_decision["should_pause"] is True
    interrupt_id = next(iter(pending))
    saved_state = execution.save()

    restored = flow.create_execution(auto_close=False)
    restored.load(saved_state)
    await restored.async_continue_with(
        interrupt_id,
        {
            "approved": True,
            "reviewer": "legal-director",
            "comment": "Proceed, but keep the termination and acceptance risks visible.",
        },
    )

    state = await restored.async_close()
    print("[FINAL_REPORT]")
    pprint(state["final_report"])

    assert state["reviewed"]["resume"]["interrupt_id"] == "model-decided-legal-review"
    assert state["final_report"]["human_approval_required"] is True


if __name__ == "__main__":
    asyncio.run(main())


# Expected key output with DeepSeek or local Ollama configured:
# [MODEL_PROVIDER] prints deepseek or ollama.
# [MODEL_DECISION] shows should_pause: True and risk_level: high.
# [PENDING_INTERRUPT] contains model-decided-legal-review.
# [FINAL_REPORT] shows approved_after_human_review and human_approval_required: True.

# How it works:
# The LLM acts as a policy engine: model_assess_document asks the model whether the contract
# warrants human review.  The model returns should_pause=True for high-risk issues.
# autonomous_pause_gate checks this flag and calls async_pause_for(resume_to="self") —
# "self" means the same chunk handles both the initial pause decision and the resume branch,
# distinguished by data.is_resume.  On resume, data.resume.value carries the approval dict
# and data.resume.origin_signal carries the original interrupt metadata.
# The execution is serialized with execution.save() and resumed on a separate execution object
# to simulate the cross-session (e.g. web-request) boundary.
#
# Flow:
# async_start(DOCUMENT_FOR_REVIEW)
#   |
#   v
# model_assess_document  ->  calls LLM, state["model_decision"] = {should_pause:True, risk:"high", …}
#   |
#   v
# autonomous_pause_gate  ->  decision.should_pause is True
#                             async_pause_for(type="model_decided_legal_review",
#                                             interrupt_id="model-decided-legal-review",
#                                             resume_to="self")  [PAUSED]
#   |
# execution.save()  ->  saved_state
# [--- cross-session boundary ---]
# restored.load(saved_state)
# restored.async_continue_with("model-decided-legal-review", {approved:True, reviewer:…})
#   |
#   v  (same autonomous_pause_gate chunk, data.is_resume=True)
# autonomous_pause_gate  ->  state["reviewed"] = {status:"approved_after_human_review", …}
#   |
#   v
# finalize  ->  state["final_report"] = {status, risk, human_approval_required:True}
#   |
# async_close()
