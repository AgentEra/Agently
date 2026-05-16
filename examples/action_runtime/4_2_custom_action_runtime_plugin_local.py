from _shared import configure_deepseek, print_action_results, print_response
from agently import Agently
from agently.builtins.plugins.ActionRuntime import AgentlyActionRuntime
from agently.core import PluginManager
from agently.utils import FunctionShifter, Settings


def normalize_title(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def count_words(text: str) -> int:
    return len(text.split())


def extract_title(raw_text: str) -> str:
    parts = raw_text.split("`")
    if len(parts) >= 3:
        return parts[1]
    return raw_text


class TitlePlanningActionRuntime:
    name = "TitlePlanningActionRuntime"
    DEFAULT_SETTINGS = {}

    def __init__(self, *, action, plugin_manager, settings):
        self.action = action
        self._builtin = AgentlyActionRuntime(
            action=action,
            plugin_manager=plugin_manager,
            settings=settings,
        )
        self._planning_handler = self._default_planning_handler

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    def register_action_planning_handler(self, handler):
        if handler is None:
            self._planning_handler = self._default_planning_handler
        else:
            self._planning_handler = FunctionShifter.asyncify(handler)
        return self

    def register_action_execution_handler(self, handler):
        self._builtin.register_action_execution_handler(handler)
        return self

    def resolve_planning_handler(self, handler=None):
        selected = handler if handler is not None else self._planning_handler
        return FunctionShifter.asyncify(selected)

    def resolve_execution_handler(self, handler=None):
        return self._builtin.resolve_execution_handler(handler)

    def resolve_planning_protocol(self, settings, planning_protocol=None):
        _ = settings
        return planning_protocol or "custom_title_runtime"

    async def _default_planning_handler(
        self,
        context,
        request,
    ):
        _ = request
        prompt = context["prompt"]
        done_plans = context.get("done_plans", [])
        if len(done_plans) == 0:
            title = extract_title(str(prompt.get("input", "")))
            return {
                "next_action": "execute",
                "action_calls": [
                    {
                        "purpose": "Normalize the input title before counting words",
                        "action_id": "normalize_title",
                        "action_input": {"text": title},
                        "todo_suggestion": "Count the words in the normalized title next",
                    }
                ],
            }
        if len(done_plans) == 1:
            normalized_title = str(done_plans[0].get("result", ""))
            return {
                "next_action": "execute",
                "action_calls": [
                    {
                        "purpose": "Count the words in the normalized title",
                        "action_id": "count_words",
                        "action_input": {"text": normalized_title},
                        "todo_suggestion": "Return the normalized title and word count",
                    }
                ],
            }
        return {
            "next_action": "response",
            "action_calls": [],
        }

    async def async_generate_action_call(
        self,
        *,
        prompt,
        settings,
        action_list,
        agent_name="Manual",
        planning_handler=None,
        done_plans=None,
        last_round_records=None,
        round_index=0,
        max_rounds=None,
        planning_protocol=None,
    ):
        handler = self.resolve_planning_handler(planning_handler)
        decision = await handler(
            {
                "prompt": prompt,
                "settings": settings,
                "agent_name": agent_name,
                "round_index": round_index,
                "max_rounds": max_rounds,
                "done_plans": done_plans if isinstance(done_plans, list) else [],
                "last_round_records": last_round_records if isinstance(last_round_records, list) else [],
                "action": self.action,
                "runtime": self,
            },
            {
                "action_list": action_list,
                "planning_protocol": planning_protocol,
            },
        )
        normalized = self.action._normalize_action_decision(decision)
        commands = normalized.get("action_calls", [])
        return commands if isinstance(commands, list) else []

    async def async_generate_tool_command(self, **kwargs):
        return await self.async_generate_action_call(
            prompt=kwargs["prompt"],
            settings=kwargs["settings"],
            action_list=kwargs["tool_list"],
            agent_name=kwargs.get("agent_name", "Manual"),
            planning_handler=kwargs.get("plan_analysis_handler"),
            done_plans=kwargs.get("done_plans"),
            last_round_records=kwargs.get("last_round_records"),
            round_index=kwargs.get("round_index", 0),
            max_rounds=kwargs.get("max_rounds"),
            planning_protocol="custom_title_runtime",
        )


if __name__ == "__main__":
    configure_deepseek()
    settings = Settings(name="custom-action-runtime-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="custom-action-runtime-plugin-manager",
    )
    plugin_manager.register("ActionRuntime", TitlePlanningActionRuntime)

    agent = Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="custom-action-runtime-agent",
    )
    agent.set_agent_prompt(
        "system",
        "Use the custom action runtime to normalize the title and count words before replying.",
    )
    agent.set_action_loop(max_rounds=3)
    agent.action.register_action(
        action_id="normalize_title",
        desc="Normalize whitespace, trim the edges, and convert the title to lowercase.",
        kwargs={"text": (str, "Title text to normalize.")},
        func=normalize_title,
        expose_to_model=True,
    )
    agent.action.register_action(
        action_id="count_words",
        desc="Count how many words are in the given text.",
        kwargs={"text": (str, "Text to count.")},
        func=count_words,
        expose_to_model=True,
    )

    agent.use_actions(["normalize_title", "count_words"])
    agent.input(
        "Use actions on this title: `  Action   Runtime   Plugin   Refactor  `. "
        "Then answer with the normalized title and word count."
    )
    records = agent.get_action_result(max_rounds=3)
    print_action_results(records)
    response = agent.get_response()
    print_response(response)

# Expected key output after configuring DeepSeek:
# The custom ActionRuntime plans normalize_title first, then count_words.
# The normalized title is "action runtime plugin refactor".
# The exact word count is 4.
