from _shared import configure_deepseek, print_action_results, print_response
from agently import Agently
from agently.core import PluginManager
from agently.utils import Settings


def multiply(a: float, b: float) -> float:
    return a * b


class SingleRoundActionFlow:
    name = "SingleRoundActionFlow"
    DEFAULT_SETTINGS = {}

    def __init__(self, *, plugin_manager, settings):
        self.plugin_manager = plugin_manager
        self.settings = settings

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    async def async_run(
        self,
        *,
        action,
        prompt,
        settings,
        action_list,
        agent_name="Manual",
        parent_run_context=None,
        planning_handler=None,
        execution_handler=None,
        max_rounds=None,
        concurrency=None,
        timeout=None,
        planning_protocol=None,
    ):
        _ = (parent_run_context, timeout)
        if planning_handler is None or execution_handler is None:
            raise RuntimeError("SingleRoundActionFlow requires planning and execution handlers.")

        decision = action._normalize_action_decision(
            await planning_handler(
                {
                    "prompt": prompt,
                    "settings": settings,
                    "agent_name": agent_name,
                    "round_index": 0,
                    "max_rounds": max_rounds or 1,
                    "done_plans": [],
                    "last_round_records": [],
                    "action": action,
                    "runtime": action.action_runtime,
                },
                {
                    "action_list": action_list,
                    "planning_protocol": planning_protocol,
                },
            )
        )
        commands = decision.get("action_calls", [])
        if not isinstance(commands, list) or len(commands) == 0:
            return []

        raw_records = await execution_handler(
            {
                "prompt": prompt,
                "settings": settings,
                "agent_name": agent_name,
                "round_index": 0,
                "max_rounds": max_rounds or 1,
                "done_plans": [],
                "last_round_records": [],
                "action": action,
                "runtime": action.action_runtime,
            },
            {
                "action_calls": commands,
                "async_call_action": action.async_call_action,
                "concurrency": concurrency,
            },
        )
        return action._normalize_execution_records(raw_records, commands)


if __name__ == "__main__":
    configure_deepseek()
    settings = Settings(name="custom-action-flow-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="custom-action-flow-plugin-manager",
    )
    plugin_manager.register("ActionFlow", SingleRoundActionFlow)

    agent = Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="custom-action-flow-agent",
    )
    agent.set_agent_prompt(
        "system",
        "Use the custom single-round action flow for exact multiplication before replying.",
    )
    agent.action.register_action(
        action_id="multiply",
        desc="Multiply two numbers.",
        kwargs={"a": (float, "First number."), "b": (float, "Second number.")},
        func=multiply,
        expose_to_model=True,
    )

    async def planning_handler(
        context,
        request,
    ):
        _ = (context, request)
        return {
            "next_action": "execute",
            "action_calls": [
                {
                    "purpose": "Multiply 12.5 by 4 using the custom flow",
                    "action_id": "multiply",
                    "action_input": {"a": 12.5, "b": 4},
                    "todo_suggestion": "Return the multiplication result",
                }
            ],
        }

    agent.register_action_planning_handler(planning_handler)
    agent.use_actions("multiply")
    agent.input("Use the multiply action once, then answer with the exact product.")
    records = agent.get_action_result(max_rounds=1)
    print_action_results(records)
    response = agent.get_response()
    print_response(response)

# Expected key output after configuring DeepSeek:
# [ACTION_RECORDS] includes one successful multiply call from SingleRoundActionFlow.
# The product of 12.5 and 4 is 50.0.
# [MODEL_REPLY] reports 50.0.
