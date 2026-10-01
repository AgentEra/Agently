"""Synthetic transport probes, executed unchanged against two source trees.

Only ModelRequester responses are scripted. These observations characterize
host behavior, NOT model reasoning, factual correctness or provider telemetry.
Run this file as a subprocess; never import both Agently versions together.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import mimetypes
import socket
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--api", choices=("baseline", "current"), required=True)
    parser.add_argument("--case", required=True)
    return parser.parse_args()


ARGS = arguments()
SOURCE = ARGS.source_root.resolve()
sys.path.insert(0, str(SOURCE))

# Pin the environmental MIME dependency, not the observed artifact field.
# OS databases can add .md on Linux while the same Python on macOS lacks it.
# Use the interpreter's built-in table for both immutable-old and current runs.
mimetypes.guess_type = mimetypes.MimeTypes().guess_type

import agently  # noqa: E402
from agently import Agently  # noqa: E402
from agently.core import PluginManager  # noqa: E402
from agently.core.application.AgentTask import AgentTask  # noqa: E402
from agently.types.data import AgentlyRequestData  # noqa: E402
from agently.utils import DataFormatter, Settings  # noqa: E402

assert Path(agently.__file__).resolve() == SOURCE / "agently" / "__init__.py", "Source isolation failed"

IDENTITIES: dict[str, str] = {}


def bind_identity(value: str, family: str) -> None:
    if value and value not in IDENTITIES:
        count = sum(label.startswith(f"<{family}:") for label in IDENTITIES.values())
        IDENTITIES[value] = f"<{family}:{count + 1}>"


@dataclass
class TransportReply:
    content: Any
    finish_reason: str = "stop"


class ScriptedRequester:
    name = "ExecutionCharacterizationRequester"
    DEFAULT_SETTINGS: dict[str, Any] = {}
    script: list[Any] = []
    requests: list[dict[str, Any]] = []
    dispatches = 0

    def __init__(self, prompt: Any, settings: Any) -> None:
        self.prompt = prompt
        self.settings = settings

    @staticmethod
    def _on_register() -> None:
        pass

    @staticmethod
    def _on_unregister() -> None:
        pass

    def generate_request_data(self) -> AgentlyRequestData:
        index = len(self.requests)
        # A rendered request may be rejected by a host budget before dispatch.
        self.requests.append({
            "prompt": DataFormatter.sanitize(self.prompt.get()),
            "prompt_text": self.prompt.to_text(),
        })
        return AgentlyRequestData(
            client_options={}, headers={}, data={"index": index},
            request_options={"stream": True}, request_url="mock://execution-characterization",
        )

    async def request_model(self, request_data: AgentlyRequestData) -> Any:
        index = type(self).dispatches
        type(self).dispatches += 1
        if index >= len(self.script):
            raise AssertionError(f"Unexpected provider dispatch {index + 1}; script exhausted")
        value = self.script[index]
        if isinstance(value, Exception):
            raise value
        reply = value if isinstance(value, TransportReply) else TransportReply(value)
        self.finish_reason = reply.finish_reason
        value = reply.content(self.prompt) if callable(reply.content) else reply.content
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        midpoint = max(1, len(content) // 2)
        yield "message", content[:midpoint]
        await asyncio.sleep(0)
        yield "message", content[midpoint:]

    async def broadcast_response(self, response_generator: Any) -> Any:
        content = ""
        async for _event, data in response_generator:
            content += data
            yield "delta", data
        yield "done", content
        meta: dict[str, Any] = {"provider": "synthetic-transport", "status": "completed", "finish_reason": self.finish_reason}
        if self.finish_reason == "length":
            meta.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        yield "meta", meta


def create_agent(root: Path, responses: list[Any]) -> Any:
    ScriptedRequester.script = responses
    ScriptedRequester.requests = []
    ScriptedRequester.dispatches = 0
    settings = Settings(name="characterization-settings", parent=Agently.settings)
    settings.set("debug", False)
    manager = PluginManager(settings, parent=Agently.plugin_manager, name="characterization-plugins")
    manager.register("ModelRequester", ScriptedRequester, activate=True)
    return Agently.AgentType(manager, parent_settings=settings, name="characterization").use_task_workspace(
        root / "workspace", mode="read_write",
    )


def producer(agent: Any, name: str, *, limits: Any = None) -> Any:
    # The sole API migration adapter. Both spellings select the same scenario;
    # emitted names/events/Prompts remain unmodified in the observed output.
    if ARGS.api == "baseline":
        return agent.create_execution(limits=limits).pattern(name)
    return agent.create_execution(name, limits=limits)


def error_record(error: Exception) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error)}


async def outcome(execution: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        return {"value": await execution.async_get_data(**kwargs)}
    except Exception as error:
        return {"error": error_record(error)}


def stream_record(execution: Any) -> list[dict[str, Any]]:
    # Full ordered public path/event/delta projection, not timing or private
    # provider metadata. Result/envelope and policy payloads are captured below.
    return [{"path": item.path, "event": item.event_type, "delta": item.delta,
             "is_complete": item.is_complete, "completion_source": item.completion_source}
            for item in execution.stream.items]


def record(execution: Any, result: Any, **extra: Any) -> dict[str, Any]:
    bind_identity(execution.id, "EXECUTION")
    bind_identity(execution.task_workspace.task_workspace_id, "TASK_WORKSPACE")
    # Only bind actual host-issued Action call ids at the ActionRuntime port.
    for request in ScriptedRequester.requests:
        for slot_name in ("input", "info"):
            slot = request["prompt"].get(slot_name)
            if not isinstance(slot, dict):
                continue
            pack = slot.get("context_pack", {})
            if pack:
                bind_identity(pack["package_id"], "CONTEXT_PACKAGE")
                for key in pack.get("source_coverage", {}):
                    if key.startswith(("record_store_binding:", "task_workspace_binding:")):
                        bind_identity(key.split(":", 1)[1], "CONTEXT")
            diagnostics = slot.get("execution_meta", {}).get("diagnostics", {})
            for item in slot.get("taskboard_acceptance_index", {}).get("items", []):
                if item.get("source") == "taskboard_card":
                    fingerprint = item["verdict_fingerprint"]
                    assert fingerprint.startswith("sha256:") and len(fingerprint) == 71
                    assert all(char in "0123456789abcdef" for char in fingerprint[7:])
                    # This specific advisory cache key hashes full card runtime
                    # metadata, including fresh run ids. It is not a file digest.
                    bind_identity(fingerprint, "CARD_VERDICT_FINGERPRINT")
            scope = diagnostics.get("action_artifact_release", {}).get("scope", {})
            if scope.get("id"):
                bind_identity(scope["id"], "CHILD_EXECUTION")
            for event in diagnostics.get("stages", {}).get("events", []):
                for container in (event, event.get("meta", {})):
                    for key in ("execution_id", "model_run_id", "request_run_id", "response_id", "run_id"):
                        if container.get(key):
                            bind_identity(container[key], "RUN")
        info = request["prompt"].get("info", [])
        for entry in info if isinstance(info, list) else []:
            for call in entry.get("action_runtime", {}).get("round_state", {}).get("last_round_result", []):
                bind_identity(call["action_call_id"], "ACTION_CALL")
    return {
        "outcome": result, "status": execution.status,
        "route": execution.route_info.get("selected_route"),
        "events": stream_record(execution),
        "requests": list(ScriptedRequester.requests),
        "dispatches": ScriptedRequester.dispatches,
        "artifacts": execution.artifact_results,
        "reviews": execution.review_results,
        **extra,
    }


READY = {"plan_ready": True, "planning_goal": "Plan a release.",
         "final_deliverable": "A release plan.", "readiness_summary": "Inputs are sufficient.", "questions": []}
NOT_READY = {**READY, "plan_ready": False, "readiness_summary": "Environment is missing.",
             "questions": [{"question": "Which environment?", "why_needed": "Choose the deployment steps."}]}
PLAN = {"steps": ["check", "deploy"]}
PLAN_SCHEMA = {"steps": [(str, "An ordered step.", True)]}
CONTENT = [
    {"document_title": "Guide", "sections": [
        {"section_id": "context", "title": "Context", "brief": "State the context."},
        {"section_id": "checks", "title": "Checks", "brief": "List the checks."},
    ]},
    {"body": "First body — 中文。", "continuity_note": "Use the term Deployment."},
    {"body": "Second body.", "continuity_note": ""},
]

# Same business bytes; only the approved current producer's private contracts differ.
CURRENT_CONTENT = [
    {"document_title": "Guide", "part_plan": [
        {"part_title": "Context", "part_brief": "State the context."},
        {"part_title": "Checks", "part_brief": "List the checks."},
    ]},
    {"body": "First body — 中文。"},
    {"summary": "First body — 中文。"},
    {"body": "Second body."},
]


async def probe(case: str, root: Path) -> dict[str, Any]:
    if case in {"direct_text", "direct_json", "concurrent_readers", "mutation_and_fresh", "provider_failure"}:
        response: Any = {"reply": "完成", "count": 2} if case == "direct_json" else "A — 中文 [info.source] {output.reply}"
        script: list[Any] = [RuntimeError("synthetic provider failure")] if case == "provider_failure" else [response]
        if case == "mutation_and_fresh":
            script.append("second reply")
        agent = create_agent(root, script)
        execution = agent.input("Original source").info({"source": "Declared fact"}).instruct("Refer to [info.source].")
        if case == "direct_json":
            execution.output({"reply": (str, "Reply", True), "count": (int, "Count", True)}, format="json")
        captured = execution.get_result()
        if case == "concurrent_readers":
            results = await asyncio.gather(execution.async_start(), captured.async_get_data(), execution.async_get_full_data())
            return record(execution, results, reread=await captured.async_get_data())
        result = await outcome(execution, max_retries=0)
        if case == "provider_failure":
            return record(execution, result, reread=await outcome(execution, max_retries=0))
        if case == "mutation_and_fresh":
            errors = []
            for mutate in (lambda: execution.input("forbidden"), lambda: execution.output(str), lambda: execution.info("forbidden")):
                try:
                    mutate()
                except Exception as error:
                    errors.append(error_record(error))
            fresh = execution.create_execution().input("second source")
            second = await outcome(fresh)
            return record(execution, result, mutation_errors=errors, fresh_prompt=fresh.prompt_snapshot,
                          fresh_result=second, distinct_identity=fresh.id != execution.id)
        return record(execution, result, full=await execution.async_get_full_data(),
                      text=await captured.async_get_text(), reread=await captured.async_get_data())

    if case == "fluent_isolation":
        agent = create_agent(root, [{"reply": "first"}, {"reply": "second"}])
        agent.system("Persistent system.", always=True)
        first = agent.input("first").info("only-first").output({"reply": (str,)})
        first_value = first.start()
        agent.agent_prompt.set("chat_history", [{"role": "user", "content": "New history"}])
        second = agent.input("second").output({"reply": (str,)})
        second_value = second.start()
        return record(second, [first_value, second_value], first_prompt=first.prompt_snapshot,
                      second_prompt=second.prompt_snapshot, distinct_identity=first.id != second.id)

    if case in {"validation_repair", "validation_exhaustion"}:
        responses = [{"count": 1}, {"count": 2}] if ARGS.api == "baseline" else [{"count": 1}]
        # Execution observes complete structured fields before final validation;
        # accepted instant semantics prohibit a provider replay after that point.
        agent = create_agent(root, responses)
        calls = []

        def validate(value: Any, context: Any) -> bool:
            calls.append({"value": value, "parsed": context.parsed_result})
            return value["count"] == 2 and case != "validation_exhaustion"

        execution = (agent.input("Return a count of 2.").output({"count": (int, "Must equal 2.", True)})
                     .validate(validate))
        result = await outcome(execution, max_retries=1)
        return record(execution, result, validation_calls=calls, reread=await outcome(execution, max_retries=1))

    if case in {"review_warn", "review_block", "artifact_review", "artifact_escape", "default_review_artifact"}:
        responses: list[Any] = ["candidate body — 中文"]
        if case == "default_review_artifact":
            responses.append({"passed": True, "quality_level": "adequate", "checks": [],
                              "summary": "Synthetic judgment.", "issues": [], "overall_suggestions": []})
        agent = create_agent(root, responses)
        contexts = []

        def review(value: Any, context: Any) -> bool:
            contexts.append({"value": value, "prompt": context.prompt, "goals": context.goals,
                             "rules": context.rules, "on_fail": context.on_fail,
                             "artifact_refs": context.artifact_refs})
            return case == "artifact_review"

        execution = agent.input("Write a report using the source.").info({"source": "A declared fact."})
        if case == "default_review_artifact":
            execution.artifact("report.md").review()
        elif case == "artifact_escape":
            execution.artifact(root / "outside.md")
        else:
            execution.review(review, on_fail="block" if case == "review_block" else "warn")
            # Declaration order is intentionally opposite to execution order.
            if case in {"artifact_review", "review_block"}:
                execution.artifact("report.md")
        result = await outcome(execution, max_retries=0)
        files = {str(path.relative_to(root)): path.read_text() for path in root.rglob("*.md")}
        return record(execution, result, review_calls=contexts, files=files, reread=await outcome(execution, max_retries=0))

    if case in {"plan_ready", "plan_clarification", "plan_budget", "plan_validation"}:
        responses = [NOT_READY, READY, PLAN] if case == "plan_clarification" else [READY, PLAN]
        if case == "plan_budget":
            responses = [READY]
        agent = create_agent(root, responses)
        exchanges = []
        checks = []

        async def clarify(exchange: Any) -> str:
            bind_identity(exchange["exchange_id"], "EXCHANGE")
            exchanges.append({"request": exchange["request"]})
            return "Staging environment."

        def validate(value: Any, context: Any) -> bool:
            checks.append({"value": value, "max_retries": context.max_retries})
            return True

        limits = {"max_model_requests": 1} if case == "plan_budget" else None
        execution = producer(agent, "plan", limits=limits).input("Plan the release.").output(PLAN_SCHEMA)
        if case == "plan_clarification":
            execution.interact(clarify)
        if case == "plan_validation":
            execution.validate(validate)
        result = await outcome(execution, max_retries=0)
        return record(execution, result, exchanges=exchanges, validation_calls=checks)

    if case in {"long_content", "long_content_rejected"}:
        agent = create_agent(root, list(CONTENT if ARGS.api == "baseline" else CURRENT_CONTENT))
        checks = []

        def validate(value: Any, context: Any) -> bool:
            checks.append({"value": value, "max_retries": context.max_retries})
            return case == "long_content"

        execution = producer(agent, "long_content").input("Write a two-section guide.")
        execution.validate(validate).artifact("guide.md")
        result = await outcome(execution, max_retries=2)
        files = {str(path.relative_to(root)): path.read_text() for path in root.rglob("*.md")}
        return record(execution, result, validation_calls=checks, files=files, reread=await outcome(execution))

    if case == "action_once":
        script = [
            {"next_action": "execute", "response": None, "execution_commands": [
                {"purpose": "Record an authorized effect.", "action_id": "write_marker", "action_input": {"value": "marker"}},
            ]},
            {"next_action": "response", "execution_commands": [], "response": "action result"},
        ]
        agent = create_agent(root, script)
        calls = []

        @agent.action_func
        def write_marker(value: str) -> str:
            """Write one marker into the test-owned workspace."""
            calls.append(value)
            with (root / "effect.txt").open("a", encoding="utf-8") as target:
                target.write(value + "\n")
            return value

        execution = agent.input("Call write_marker once with value marker.").use_actions("write_marker")
        execution.review(lambda _value, _context: False, on_fail="block")
        result = await outcome(execution, max_retries=0)
        second = await outcome(execution, max_retries=0)
        return record(execution, result, reread=second, action_calls=calls,
                      effect=(root / "effect.txt").read_text() if (root / "effect.txt").exists() else None)

    if case in {"resume_flat", "resume_taskboard"}:
        # Deliberately seeded *terminal* state: tests persistence and readers,
        # not task production. The real snapshot is saved and loaded via RecordStore.
        agent = create_agent(root, [])
        agent.use_record_store(root / "records", mode="read_write")
        task = AgentTask(
            agent, task_id="characterization-terminal", goal="Retained goal",
            success_criteria=["Retained criterion"], execution=case.removeprefix("resume_"),
            options={"agent_task": {"record_store_recovery": True},
                     "execution_prompt_snapshot": {"input": "Original request", "info": {"source": "Original fact"}}},
        )
        await task._write_resume_snapshot(1, {"is_complete": True, "final_result": "retained business value"})
        assert not task.diagnostics.get("resume_snapshot_errors"), task.diagnostics
        execution = await agent.async_resume(task.id)
        reviews = []

        def review(value: Any, context: Any) -> bool:
            reviews.append({"value": value, "prompt": context.prompt, "goals": context.goals})
            return True

        execution.review(review)
        result = await outcome(execution)
        full = await execution.async_get_full_data()
        return record(execution, result, full=full, review_calls=reviews, reread=await outcome(execution))

    if case == "goal_direct":
        agent = create_agent(root, ["goal answer"])
        execution = agent.input("Source").goal("Explain the source", ["State the assumption"]).strategy("direct")
        return record(execution, await outcome(execution))

    if case in {"ensure_short", "ensure_continuation", "ensure_stale", "ensure_task_conflict"}:
        def continuation(prompt: Any) -> dict[str, Any]:
            # Protocol echo only; all business bytes and decisions are authored
            # in this synthetic script, never inferred from a Prompt keyword.
            supplied = prompt.get("input")
            if ARGS.api == "current":
                supplied = json.loads(supplied)
            state = supplied["long_output_continuation"]
            return {"base_revision": state["base_revision"],
                    "base_digest": "0" * 64 if case == "ensure_stale" else state["base_digest"],
                    "anchor": state["anchor"], "updates": [
                        {"path_key": "$text", "operation": "append_text",
                         "unit_index": state["assembly_slots"][0]["next_unit_index"], "value": "tail — 中文"},
                    ], "state_summary": "Synthetic transport complete.",
                    **({"is_final": True} if ARGS.api == "baseline" else {"completion": "complete"})}

        responses = [] if case == "ensure_task_conflict" else [TransportReply("prefix-", "stop")]
        if case in {"ensure_continuation", "ensure_stale"}:
            responses = [TransportReply("prefix-", "length"), TransportReply(continuation)]
        agent = create_agent(root, responses)
        execution = agent.input("Return prefix-tail — 中文.").ensure_long_output()
        if case == "ensure_task_conflict":
            execution.goal("Declared task", ["Declared criterion"])
        checks = []
        execution.validate(lambda value, _context: checks.append(value) or True)
        result = await outcome(execution, max_retries=0)
        return record(execution, result, validation_calls=checks, reread=await outcome(execution, max_retries=0))

    if case in {"flat_task", "taskboard_task"}:
        verdict = {
            "is_complete": True, "requires_block": False, "reason": "Synthetic structural verdict.",
            "failure_analysis": "", "acceptance_delta": [], "missing_criteria": [],
            "repair_constraints": [], "next_step_requirements": [], "replan_instruction": "",
            "final_result_required": True, "final_result": "task business value",
            "criterion_checks": [{"criterion_id": "criterion:1", "satisfied": True,
                                  "summary": "Synthetic criterion fixture.", "gaps": [], "evidence_ids": []}],
            "material_claim_coverage_complete": True, "material_claim_checks": [],
        }
        if case == "flat_task":
            responses = [
                {"execution_shape": "direct", "step_instruction": "Return the supplied fact.",
                 "expected_evidence": "A bounded answer.", "rationale": "One step is sufficient."},
                {"step_result": "fact", "evidence": ["Supplied fact"], "remaining_work": [],
                 "ready_for_final_verification": True, "candidate_final_result": "task business value"},
                {"selected_keys": []},
                verdict,
            ]
        else:
            responses = [
                {"board_goal": "Return the fact.", "cards": [
                    {"id": "collect", "action_block": "Summarize the fact.", "objective": "Return the fact.",
                     "depends_on": [], "evidence_to_use": [], "done_when": "The fact is summarized.",
                     "allowed_execution_shape": "model"},
                ], "reflection_points": [], "completion_gate": "The fact is summarized.",
                 "why_this_effort_shape": "One card is sufficient.", "risk_notes": []},
                {"status": "completed", "answer": "fact", "evidence": ["Supplied fact"],
                 "remaining_work": [], "diagnostics": []},
                {"accepted": True, "reason": "Synthetic synthesis.", "final_result": "task business value",
                 "missing_criteria": []},
                verdict,
            ]
            if ARGS.api != "baseline":
                # Ordinary TaskBoard answers finish at the existing finalizer;
                # a second verifier is reserved for explicit delivery contracts.
                responses = responses[:-1]
        agent = create_agent(root, responses)
        execution = agent.create_task(goal="Return the supplied fact.", success_criteria=["Return the fact."],
                                      execution="flat" if case == "flat_task" else "taskboard", max_iterations=1,
                                      task_id="characterization-task")
        result = await outcome(execution, max_retries=0)
        files = {str(path.relative_to(root)): path.read_text() for path in root.rglob("*.md")}
        return record(execution, result, full=execution.result, files=files)

    raise ValueError(f"Unknown case: {case}")


def main() -> None:
    original_connect = socket.socket.connect

    def offline_connect(channel: socket.socket, address: Any) -> Any:
        if channel.family in {socket.AF_INET, socket.AF_INET6}:
            raise AssertionError("Characterization probes prohibit network access")
        return original_connect(channel, address)

    socket.socket.connect = offline_connect
    captured = io.StringIO()
    with tempfile.TemporaryDirectory(prefix="agently-characterization-case-") as folder:
        root = Path(folder)
        with contextlib.redirect_stdout(captured):
            observed = asyncio.run(probe(ARGS.case, root))
        if ScriptedRequester.dispatches != len(ScriptedRequester.script):
            request_shapes = [request["prompt"].get("output") for request in ScriptedRequester.requests]
            raise AssertionError(f"Script consumption {ScriptedRequester.dispatches}/{len(ScriptedRequester.script)}; "
                                 f"outcome={observed['outcome']}; request_shapes={request_shapes}")
        output = {"source_root": str(SOURCE), "workspace_root": str(root), "case": ARGS.case,
                  "identities": IDENTITIES, "observed": DataFormatter.sanitize(observed)}
        print("CHARACTERIZATION:" + json.dumps(output, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
