from _shared import configure_deepseek, print_action_results, print_response
from agently import Agently
from agently.core import PluginManager
from agently.utils import Settings


class ReverseTextActionExecutor:
    name = "ReverseTextActionExecutor"
    DEFAULT_SETTINGS = {}
    kind = "custom_reverse"
    sandboxed = False

    @staticmethod
    def _on_register():
        pass

    @staticmethod
    def _on_unregister():
        pass

    async def execute(self, *, spec, action_call, policy, settings):
        _ = (spec, policy, settings)
        action_input = action_call.get("action_input", {})
        if not isinstance(action_input, dict):
            action_input = {}
        text = str(action_input.get("text", ""))
        return {
            "reversed_text": text[::-1],
            "length": len(text),
        }


if __name__ == "__main__":
    configure_deepseek()
    settings = Settings(name="custom-action-executor-settings", parent=Agently.settings)
    plugin_manager = PluginManager(
        settings,
        parent=Agently.plugin_manager,
        name="custom-action-executor-plugin-manager",
    )
    plugin_manager.register("ActionExecutor", ReverseTextActionExecutor, activate=False)

    agent = Agently.AgentType(
        plugin_manager,
        parent_settings=settings,
        name="custom-action-executor-agent",
    )
    agent.set_agent_prompt(
        "system",
        "Use the custom reverse_text action to produce exact string-processing results before replying.",
    )
    agent.action.register_action(
        action_id="reverse_text",
        desc="Reverse the input text and report its length.",
        kwargs={"text": (str, "Text to reverse.")},
        executor=agent.action.create_action_executor("ReverseTextActionExecutor"),
        expose_to_model=True,
    )

    agent.use_actions("reverse_text")
    agent.input("Use the reverse_text action on `Action Runtime`, then answer with the reversed text and length.")
    records = agent.get_action_result()
    print_action_results(records)
    response = agent.get_response()
    print_response(response)

# Expected key output after configuring DeepSeek:
# [ACTION_RECORDS] includes a successful reverse_text call.
# The custom executor returns reversed_text="emitnuR noitcA" and length=14.
# [MODEL_REPLY] reports the reversed text and length.
