"""Low-level mixed sync/async runtime probe; no model call is required.

Expected key output from a real local run:
result: ROUTING
steps: ['provider', 'state']

TriggerFlow async runtime
    -> synchronous chunk
    -> provider-owned ``with Stage()``
    -> asynchronous provider method
    -> synchronous TriggerFlow state facade
"""

import asyncio

from agently import TriggerFlow, TriggerFlowRuntimeData
from agently_stage import Stage


async def provider(value: str) -> str:
    await asyncio.sleep(0)
    return value.upper()


def sync_provider(value: str) -> str:
    with Stage() as stage:
        return stage.get(provider, value)


def transform(data: TriggerFlowRuntimeData):
    result = sync_provider(data.input)
    data.set_state("result", result, emit=False)
    data.set_state("steps", ["provider", "state"], emit=False)
    return result


flow = TriggerFlow(name="automatic-stage-sync-provider")
flow.to(transform)
snapshot = flow.start("routing")

print("result:", snapshot["result"])
print("steps:", snapshot["steps"])
