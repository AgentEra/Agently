# Copyright 2023-2026 AgentEra(Agently.Tech)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""ObserveBlock — artifact-aware observation folding into execution state."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from agently.builtins.blocks._protocol import FlowBlock


class ObserveBlock(FlowBlock):
    """Observation block — folds action results into execution state.

    Large artifacts (> *artifact_inline_limit* bytes) are summarized with
    hash + byte count + truncated head/tail. Smaller results are inlined
    directly into the execution state scratchpad.
    """

    name = "ObserveBlock"

    def __init__(self, *, artifact_inline_limit: int = 65536):
        self._artifact_inline_limit = artifact_inline_limit

    # ── Direct execution ──

    async def execute(
        self,
        *,
        observation: dict[str, Any],
        context: Any,
        execution: Any = None,
    ) -> dict[str, Any]:
        """Fold *observation* (an act result) into the observation state.

        If *execution* is provided (TriggerFlowExecution), the folded result
        is persisted to execution state for downstream blocks.
        """
        folded = self._fold(observation)

        await context.async_emit_runtime_stream(
            {
                "type": "block.observe",
                "action": "done",
                "payload": {
                    "act_name": observation.get("name"),
                    "inlined": folded.get("_inlined", True),
                    "result_size": len(str(observation.get("result", ""))),
                },
            }
        )

        # Persist to execution state if available
        if execution is not None:
            try:
                history = execution.get_state("observation_history") or []
                history.append(folded)
                await execution.async_set_state("observation_history", history)
                await execution.async_set_state("last_observation", folded)
            except (AttributeError, TypeError):
                pass

        return folded

    def _fold(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Fold an observation, summarizing large artifacts."""
        result = observation.get("result")
        if result is None:
            return {**observation, "_inlined": True}

        result_str = str(result)
        result_bytes = len(result_str.encode("utf-8"))

        if result_bytes <= self._artifact_inline_limit:
            return {**observation, "_inlined": True}

        # Large artifact: summarize
        sha = hashlib.sha256(result_str.encode("utf-8")).hexdigest()[:16]
        head = result_str[:4000]
        tail = result_str[-1000:] if result_bytes > 5000 else ""

        return {
            "act_type": observation.get("act_type"),
            "action_id": observation.get("action_id"),
            "name": observation.get("name"),
            "error": observation.get("error"),
            "status": observation.get("status"),
            "artifact": {
                "sha256_16": sha,
                "byte_count": result_bytes,
                "head": head,
                "tail": tail,
            },
            "_inlined": False,
        }

    # ── TriggerFlow operator builder ──

    def build_operators(
        self,
        *,
        blueprint: Any,
        context: Any,
        settings: dict[str, Any] | None = None,
    ) -> list[str]:
        cfg = settings or {}
        artifact_inline_limit = cfg.get(
            "artifact_inline_limit", self._artifact_inline_limit
        )

        block_id = f"observe-block-{uuid.uuid4().hex[:8]}"
        event_start = f"{block_id}.start"
        event_done = f"{block_id}.done"

        configured = ObserveBlock(artifact_inline_limit=artifact_inline_limit)

        async def handler(data: Any) -> Any:
            observation = data.value if isinstance(data.value, dict) else {}
            return await configured.execute(
                observation=observation,
                context=context,
                execution=data.execution,
            )

        blueprint.add_handler("event", event_start, handler, id=block_id)
        blueprint.definition.add_operator(
            id=block_id,
            kind="chunk",
            name=self.name,
            listen_signals=[
                {
                    "id": f"{block_id}.listen",
                    "trigger_type": "event",
                    "trigger_event": event_start,
                }
            ],
            emit_signals=[
                {
                    "id": f"{block_id}.emit",
                    "trigger_type": "event",
                    "trigger_event": event_done,
                }
            ],
            handler_ref={"source": "block", "block": self.name},
            options={"artifact_inline_limit": artifact_inline_limit},
        )

        return [block_id]
