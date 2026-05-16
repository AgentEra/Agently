import asyncio
from pprint import pprint
from typing import Any, cast

from agently import Agently, TriggerFlow, TriggerFlowRuntimeData


async def main():
    flow = TriggerFlow(name="example-triggerflow-managed-python")

    async def calculate(data: TriggerFlowRuntimeData):
        sandbox = cast(Any, data.require_resource("managed_python"))
        result = sandbox.run(
            "input_value = " + repr(int(data.value)) + "\n"
            "result = base + input_value\n"
        )["result"]
        data.state.set("answer", result)

    flow.to(calculate)

    result = await flow.async_start(
        2,
        execution_environments=[
            {
                "kind": "python",
                "scope": "execution",
                "resource_key": "managed_python",
                "config": {"base_vars": {"base": 40}},
            }
        ],
    )

    print("[TRIGGERFLOW_RESULT]")
    pprint(result)
    assert result == {"answer": 42}

    execution_handles = Agently.execution_environment.list(scope="execution")
    print("[EXECUTION_HANDLES_AFTER_RELEASE]")
    pprint(execution_handles)
    assert execution_handles == []


if __name__ == "__main__":
    asyncio.run(main())

# Expected key output:
# [TRIGGERFLOW_RESULT] prints {"answer": 42}.
# [EXECUTION_HANDLES_AFTER_RELEASE] prints [] after the execution-scoped Python resource is released.
