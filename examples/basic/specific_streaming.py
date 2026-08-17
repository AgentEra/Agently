from agently import Agently


# Example 1: Get Streaming Result with Function Calling
def streaming_with_function_calling():
    print("Example 1: Get Streaming Result with Function Calling\n\n-----\n\n")
    agent = Agently.create_agent()
    agent.set_settings(
        "OpenAICompatible",
        {
            "base_url": "http://localhost:11434/v1",
            "model": "qwen2.5:7b",
        },
    )

    gen = (
        agent.input("What's the weather like today in New York?")
        .options(
            {
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "Get current temperature for provided coordinates in celsius.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "latitude": {"type": "number"},
                                    "longitude": {"type": "number"},
                                },
                                "required": ["latitude", "longitude"],
                                "additionalProperties": False,
                            },
                            "strict": True,
                        },
                    }
                ],
            }
        )
        .get_generator(type="specific")
    )

    for event, content in gen:
        if event == "delta":
            print(content, end="", flush=True)
        if event == "tool_calls":
            print("\n<tool_calls>\n", content, "\n</tool_calls>")

    print("")


streaming_with_function_calling()


# Example 2: Get Streaming with Reasoning from DeepSeek
def streaming_with_reasoning_from_deepseek():
    print("Example 2: Get Streaming with Reasoning from DeepSeek\n\n-----\n\n")
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv())
    import os

    agent = Agently.create_agent()
    agent.set_settings(
        "OpenAICompatible",
        {
            "base_url": os.environ.get("DEEPSEEK_BASE_URL"),
            "model": "deepseek-v4-flash",
            "api_key": os.environ.get("DEEPSEEK_API_KEY"),
            "request_options": {"thinking": {"type": "enabled"}},
        },
    )

    gen = agent.input("What's DeepSeek? Response in English.").get_generator(type="specific")

    reasoning_done = False

    print("[Thinking]:")
    for event, content in gen:
        if event == "reasoning_delta":
            print(content, end="", flush=True)
        if event == "delta":
            if not reasoning_done:
                print("\n\n----\n\n[Reply]:\n")
                reasoning_done = True
            print(content, end="", flush=True)

    print("")


streaming_with_reasoning_from_deepseek()

# Expected output shape:
# Example 1 (function calling, local Ollama):
#   <tool_calls>
#   [{'function': {'arguments': '{"latitude":40.71,"longitude":-74.01}', 'name': 'get_weather'}, ...}]
#   </tool_calls>
# Example 2 (DeepSeek reasoning, requires API key):
#   [Thinking]: <chain-of-thought tokens>
#   ----
#   [Reply]: <answer about DeepSeek>
#
# How it works:
# get_generator(type="specific") yields (event_name, data) tuples.
# Recognized event names:
#   "delta"           — text token from the model's answer
#   "reasoning_delta" — token from the model's chain-of-thought (DeepSeek-R1 etc.)
#   "tool_calls"      — complete tool_calls payload when the model calls a function
# Example 1 passes OpenAI-style tools via .options({"tools": [...]}) and
# captures the "tool_calls" event.  Example 2 uses a reasoning model and
# separates thinking from answer by event type.
