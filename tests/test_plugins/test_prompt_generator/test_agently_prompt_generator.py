from collections.abc import Mapping
import warnings

import pytest

from agently.core import Prompt
from agently import Agently
from agently.builtins.plugins.PromptGenerator.modules.output_contract import (
    output_schema_to_json_schema,
)


def test_to_prompt_object():
    Agently.set_settings("plugins.PromptGenerator.activate", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.set("input", "OK")
    po = prompt.to_prompt_object().model_dump()
    assert po["input"] == "OK"
    assert po["chat_history"] == []
    assert po.get("output_format") == "markdown"
    prompt.set("user_info", "Nobody")
    assert prompt.to_prompt_object().model_extra == {"user_info": "Nobody"}
    po2 = prompt.to_prompt_object().model_dump()
    assert po2["input"] == "OK" and po2.get("user_info") == "Nobody"
    prompt["output"] = {"reply": (str, "your reply")}
    po3 = prompt.to_prompt_object().model_dump()
    assert po3["output"] == {"reply": (str, "your reply")}
    assert po3.get("output_format") == "json"  # Default comes from prompt settings.
    prompt.settings.set("prompt.default_output_format", "auto")
    assert prompt.to_prompt_object().output_format == "xml_field"
    prompt["output_format"] = "yaml"
    po4 = prompt.to_prompt_object().model_dump()
    assert po4["output"] == {"reply": (str, "your reply")}
    assert po4.get("output_format") == "yaml"  # You can modify it manually

    prompt_2 = Prompt(Agently.plugin_manager, Agently.settings)
    prompt_2["output_format"] = "some random words"
    prompt_2["output"] = [(str, "something")]
    # Should not be infected by `output`
    # because its value has already been set
    assert prompt_2.to_prompt_object().output_format == "some random words"


def test_to_text():
    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.update(
        {
            "input": "Hello",
            "instruct": "reply something",
            "info": "I'm a human",
            "output": {
                "thinking": (str, "describe how will you plan to reply?"),
                "reply": (str, "your final reply"),
            },
            "output_format": "json",
        }
    )
    assert (
        prompt.to_text()
        == """user:
[INFO]:
I'm a human

[INSTRUCT]:
reply something

[INPUT]:
Hello

[OUTPUT REQUIREMENT]:
Data Format: JSON
Data Structure:
{
  "thinking": <str>, // describe how will you plan to reply?
  "reply": <str> // your final reply
}

[OUTPUT]:
assistant:"""
    )


def test_prompt_slot_reference_placeholders_point_to_titles():
    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.update(
        {
            "input": {"customer": "Acme", "ticket": "T-42"},
            "info": {"policy": "Enterprise policy"},
            "instruct": "Use ${INPUT.customer} and ${INFO.policy}; obey ${OUTPUT}. Keep ${unknown} literal.",
            "output": {
                "reply": (str, "Answer using ${INSTRUCT} and ${INPUT.ticket}"),
            },
            "output_format": "json",
        }
    )

    text = prompt.to_text()

    assert "Use [INPUT > customer] and [INFO > policy]; obey [OUTPUT REQUIREMENT]." in text
    assert "Keep ${unknown} literal." in text
    assert '"reply": <str> // Answer using [INSTRUCT] and [INPUT > ticket]' in text


def test_prompt_slot_references_do_not_break_explicit_mappings():
    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.set("input", "Hello")
    prompt.set(
        "instruct",
        "Hello ${name}; see ${input.foo}.",
        mappings={"name": "Alice"},
    )

    text = prompt.to_text()

    assert "Hello Alice; see [INPUT > foo]." in text


def test_to_text_complex():
    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.update(
        {
            "input": "Hello",
            "instruct": "reply something",
            "info": "I'm a human",
            "output": {
                "thinking": (str, "describe how will you plan to reply?"),
                "reply": (str, "your final reply"),
            },
            "output_format": "json",
        }
    )
    prompt.set(
        "chat_history",
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "ni hao",
                    },
                    {
                        "type": "something",
                        "something": "OK",
                    },
                ],
            }
        ],
    )
    assert (
        prompt.to_text()
        == """user:
[CHAT HISTORY]:
[user]:ni hao

[INFO]:
I'm a human

[INSTRUCT]:
reply something

[INPUT]:
Hello

[OUTPUT REQUIREMENT]:
Data Format: JSON
Data Structure:
{
  "thinking": <str>, // describe how will you plan to reply?
  "reply": <str> // your final reply
}

[OUTPUT]:
assistant:"""
    )
    # Receive warning when using pytest test_prompt -s


def test_to_text_with_root_list_output():
    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.update(
        {
            "input": "Hello",
            "output": [
                {
                    "title": (str, "news title"),
                    "url": (str, "news url"),
                }
            ],
        }
    )
    assert (
        prompt.to_text()
        == """user:
[INPUT]:
Hello

[OUTPUT REQUIREMENT]:
Data Format: JSON
Data Structure:
[
  {
    "title": <str>, // news title
    "url": <str> // news url
  },
  ...
]

[OUTPUT]:
assistant:"""
    )


def test_empty_prompt():
    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    with pytest.raises(
        KeyError,
        match="Prompt requires at least one",
    ):
        prompt.to_text()


def test_message_prompt():
    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.update(
        {
            "input": "Hello",
            "instruct": "reply something",
            "info": "I'm a human",
            "output": {
                "thinking": (str, "describe how will you plan to reply?"),
                "reply": (str, "your final reply"),
            },
            "output_format": "json",
        }
    )
    prompt.set(
        "chat_history",
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "ni hao",
                    },
                    {
                        "type": "something",
                        "something": "OK",
                    },
                ],
            },
            {
                "role": "assistant",
                "content": "hello, what can I help you?",
            },
        ],
    )
    assert prompt.to_messages(rich_content=False) == [
        {"role": "user", "content": "ni hao"},
        {"role": "assistant", "content": "hello, what can I help you?"},
        {
            "role": "user",
            "content": '[INFO]:\nI\'m a human\n\n[INSTRUCT]:\nreply something\n\n[INPUT]:\nHello\n\n[OUTPUT REQUIREMENT]:\nData Format: JSON\nData Structure:\n{\n  "thinking": <str>, // describe how will you plan to reply?\n  "reply": <str> // your final reply\n}\n\n[OUTPUT]:',
        },
    ]
    assert prompt.to_messages(rich_content=True) == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "ni hao"},
                {"type": "something", "something": "OK"},
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "hello, what can I help you?"},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": '[INFO]:\nI\'m a human\n\n[INSTRUCT]:\nreply something\n\n[INPUT]:\nHello\n\n[OUTPUT REQUIREMENT]:\nData Format: JSON\nData Structure:\n{\n  "thinking": <str>, // describe how will you plan to reply?\n  "reply": <str> // your final reply\n}\n\n[OUTPUT]:',
                }
            ],
        },
    ]


def test_output_model():
    from typing import Literal
    from pydantic import BaseModel
    from agently.utils import DataFormatter

    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.set(
        "output",
        {
            "thinking": [(str, "Your plan to reply")],
            "reply": (
                Literal[1, 2, 3],
                "Your actually reply",
            ),
            "number": int,
        },
    )
    MyOutputModel = prompt.to_output_model()
    test_data = {"thinking": 1, "number": "123"}
    output = MyOutputModel.model_validate(test_data)
    assert DataFormatter.sanitize(prompt["output"], remain_type=True) == {
        "thinking": [(str, "Your plan to reply")],
        "reply": (
            Literal[1, 2, 3],
            "Your actually reply",
        ),
        "number": int,
    }
    assert output.model_dump()["thinking"] == ["1"]

    prompt.set("output", [(int,)])
    MyOutputModel = prompt.to_output_model()
    test_data = ["456"]
    output: "BaseModel" = MyOutputModel.model_validate({"list": test_data})
    assert output.model_dump()["list"] == [456]

    output_from_raw_list: "BaseModel" = MyOutputModel.model_validate(test_data)
    assert output_from_raw_list.model_dump()["list"] == [456]
    assert hasattr(output_from_raw_list, "root") and getattr(output_from_raw_list, "root") == [456]
    assert list(output_from_raw_list) == [456]
    assert list(output_from_raw_list)[0] == 456


def test_output_model_nested_list_serializes_validated_items_without_warning():
    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.set(
        "output",
        {
            "groups": [
                {
                    "title": (str,),
                    "options": [
                        {
                            "name": (str,),
                            "enabled": (bool,),
                        }
                    ],
                }
            ]
        },
    )
    OutputModel = prompt.to_output_model(strict_output=True)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        value = OutputModel.model_validate(
            {
                "groups": [
                    {
                        "title": "g1",
                        "options": [{"name": "a", "enabled": True}],
                    }
                ]
            }
        ).model_dump(mode="json", warnings="warn")

    assert value == {
        "groups": [
            {
                "title": "g1",
                "options": [{"name": "a", "enabled": True}],
            }
        ]
    }
    assert not [
        item
        for item in caught
        if "PydanticSerializationUnexpectedValue" in str(item.message)
    ]


def test_output_json_schema_projection_preserves_nested_list_contract():
    projected = output_schema_to_json_schema(
        {
            "title": (str, "group title", True),
            "options": [
                {
                    "name": (str, "option name", True),
                    "enabled": (bool, "selection state", True),
                }
            ],
        },
        strict_output=True,
    )

    assert projected["type"] == "object"
    assert projected["required"] == ["title", "options"]
    options = projected["properties"]["options"]
    assert options["type"] == "array"
    assert options["items"]["required"] == ["name", "enabled"]
    assert options["items"]["properties"]["enabled"]["type"] == "boolean"


def test_projected_pydantic_fragment_model_preserves_nested_constraints():
    from typing import Annotated

    from pydantic import BaseModel, Field, ValidationError

    class Net(BaseModel):
        name: str
        sinks: Annotated[list[str], Field(min_length=1, max_length=2)]

    class Package(BaseModel):
        nets: Annotated[list[Net], Field(min_length=2, max_length=2)]

    Agently.set_settings(
        "plugins.PromptGenerator.name",
        "AgentlyPromptGenerator",
    )
    source_prompt = Prompt(Agently.plugin_manager, Agently.settings)
    source_prompt.set("output", Package)
    projected_package = source_prompt.to_prompt_object().output

    fragment_prompt = Prompt(Agently.plugin_manager, Agently.settings)
    fragment_prompt.update(
        {
            "output": {"value": projected_package},
            "output_format": "json",
            "ensure_all_keys": True,
        }
    )
    FragmentModel = fragment_prompt.to_output_model(strict_output=True)

    valid = FragmentModel.model_validate(
        {
            "value": {
                "nets": [
                    {"name": "a", "sinks": ["x"]},
                    {"name": "b", "sinks": ["y", "z"]},
                ]
            }
        }
    )
    assert len(valid.model_dump()["value"]["nets"]) == 2
    with pytest.raises(ValidationError):
        FragmentModel.model_validate(
            {
                "value": {
                    "nets": [
                        {"name": "a", "sinks": []},
                        {"name": "b", "sinks": ["y"]},
                    ]
                }
            }
        )


def test_pydantic_output_model_preserves_declared_type_and_exposes_structural_prompt():
    from pydantic import BaseModel, Field

    class ActionItem(BaseModel):
        task: str

    class Ticket(BaseModel):
        title: str = Field(description="Ticket title")
        priority: int
        action_items: list[ActionItem]

    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.set("output", Ticket)

    prompt_object = prompt.to_prompt_object()

    assert isinstance(prompt_object.output, Mapping)
    assert list(prompt_object.output) == ["title", "priority", "action_items"]
    action_items_schema = prompt_object.output["action_items"][0]
    assert isinstance(action_items_schema, list)
    assert isinstance(action_items_schema[0], Mapping)
    assert list(action_items_schema[0]) == ["task"]
    assert prompt_object.output_format == "json"
    assert prompt.to_output_model() is Ticket


@pytest.mark.parametrize(
    "output_format",
    ["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal"],
)
def test_pydantic_output_constraints_are_rendered_for_every_structured_format(output_format):
    from pydantic import BaseModel, Field

    class ActionItem(BaseModel):
        name: str = Field(
            min_length=2,
            max_length=5,
            pattern=r"^[a-z]+$",
            description="item name",
        )

    class Ticket(BaseModel):
        title: str = Field(min_length=3, max_length=8, description="result title")
        priority: int = Field(ge=1, le=3, description="ticket priority")
        action_items: list[ActionItem] = Field(
            min_length=1,
            max_length=2,
            description="action items",
        )

    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.update(
        {
            "input": "Create a ticket.",
            "output": Ticket,
            "output_format": output_format,
        }
    )

    text = prompt.to_text()

    format_markers = {
        "json": "Data Format: JSON",
        "flat_markdown": "Data Format: Structured Markdown",
        "hybrid": "Data Format: Structured Markdown with JSON blocks",
        "xml_field": "Data Format: XML-like field envelope",
        "yaml_literal": "Data Format: YAML literal document",
    }
    assert format_markers[output_format] in text
    assert "Field requirements:" in text
    assert "- title: result title; required; must not be null; length: 3..8 characters" in text
    assert "- priority: ticket priority; required; must not be null; value: >= 1 and <= 3" in text
    assert "- action_items: action items; required; must not be null; item count: 1..2" in text
    assert (
        "- action_items[*].name: item name; required; must not be null; " "length: 2..5 characters; pattern: ^[a-z]+$"
    ) in text


def test_pydantic_output_contract_renders_alias_nullability_literal_and_numeric_rules():
    from typing import Literal
    from pydantic import BaseModel, Field

    class Ticket(BaseModel):
        identifier: str = Field(alias="ticket_id", min_length=1)
        note: str | None
        status: Literal["open", "closed"]
        confidence: float = Field(gt=0, lt=1, multiple_of=0.1)

    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.update(
        {
            "input": "Create a ticket.",
            "output": Ticket,
            "output_format": "json",
        }
    )

    text = prompt.to_text()

    assert "- ticket_id: required; must not be null; length: >= 1 characters" in text
    assert "- note: required; null is allowed" in text
    assert '- status: required; must not be null; allowed values: ["open","closed"]' in text
    assert "- confidence: required; must not be null; value: > 0 and < 1; multiple of 0.1" in text


@pytest.mark.parametrize(
    "output_format",
    ["json", "flat_markdown", "hybrid", "xml_field", "yaml_literal"],
)
def test_tuple_ensure_policies_are_rendered_for_every_structured_format(output_format):
    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.update(
        {
            "input": "Decide whether to continue.",
            "output": {
                "ready": (bool, "whether ready", True),
                "reply": (str, "final reply", "not_null"),
            },
            "output_format": output_format,
        }
    )

    text = prompt.to_text()

    assert "- ready: whether ready; required key; null or empty values are allowed" in text
    assert "- reply: final reply; required key; value must not be null, blank, or empty" in text


def test_serializable_output_prompt_preserves_not_null_ensure_marker():
    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.set(
        "output",
        {
            "ready": (bool, "whether the plan can proceed", True),
            "questions": (list, "clarification questions if needed", "not_null"),
        },
    )

    serialized = prompt.to_serializable_prompt_data()
    output = serialized["output"]
    assert isinstance(output, Mapping)
    ready = output["ready"]
    questions = output["questions"]
    assert isinstance(ready, Mapping)
    assert isinstance(questions, Mapping)

    assert ready["$ensure"] is True
    assert questions["$ensure"] == "not_null"


def test_output_model_supports_enum():
    from enum import Enum, IntEnum
    import json

    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)

    class TicketStatus(Enum):
        OPEN = "open"
        CLOSED = "closed"

    class TicketPriority(IntEnum):
        LOW = 1
        HIGH = 2

    prompt.set(
        "output",
        {
            "status": (TicketStatus, "ticket status"),
            "priority": (TicketPriority, "ticket priority"),
        },
    )

    assert (
        prompt.to_text()
        == """user:
[OUTPUT REQUIREMENT]:
Data Format: JSON
Data Structure:
{
  "status": <Literal[open, closed]>, // ticket status
  "priority": <Literal[1, 2]> // ticket priority
}

[OUTPUT]:
assistant:"""
    )

    assert json.loads(prompt.to_json_prompt())["output"] == {
        "status": {
            "$type": "Literal[open, closed]",
            "$desc": "ticket status",
        },
        "priority": {
            "$type": "Literal[1, 2]",
            "$desc": "ticket priority",
        },
    }

    OutputModel = prompt.to_output_model()
    output_by_value = OutputModel.model_validate({"status": "open", "priority": 1}).model_dump()
    assert output_by_value["status"] == TicketStatus.OPEN
    assert output_by_value["priority"] == TicketPriority.LOW

    output_by_name = OutputModel.model_validate({"status": "OPEN", "priority": "HIGH"}).model_dump()
    assert output_by_name["status"] == TicketStatus.OPEN
    assert output_by_name["priority"] == TicketPriority.HIGH


def test_hybrid_prompt_marks_sanitized_scalar_fields_as_text():
    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.set(
        "output",
        {
            "analysis": (str, "One paragraph analysis.", True),
            "items": [{"name": (str, "Item name.", True)}],
        },
    )
    prompt.set("output_format", "hybrid")

    text = prompt.to_text()

    assert "- analysis: text; One paragraph analysis." in text
    assert "### analysis\n(your content here)" in text
    assert (
        "### items\n"
        "```json\n"
        "[\n"
        "  {\n"
        "    \"name\": \"...\"\n"
        "  }\n"
        "]\n"
        "```"
    ) in text


def test_rich_prompt():
    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.update(
        {
            "attachment": [
                {
                    "type": "text",
                    "text": "你好",
                },
                {
                    "type": "image_url",
                    "image_url": "http://example.com",
                },
            ]
        }
    )
    messages = prompt.to_messages(rich_content=True)
    assert messages[0] == {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "你好",
            },
            {
                "type": "image_url",
                "image_url": "http://example.com",
            },
        ],
    }
    messages = prompt.to_messages()
    assert messages[0] == {
        "role": "user",
        "content": "你好",
    }


def test_strict_role_orders():
    Agently.set_settings("plugins.PromptGenerator.name", "AgentlyPromptGenerator")
    prompt = Prompt(Agently.plugin_manager, Agently.settings)
    prompt.set(
        "chat_history",
        [
            {"role": "assistant", "content": "Hi, how can I help you today?"},
            {"role": "user", "content": "?"},
        ],
    )
    prompt.set("input", "hi")
    messages = prompt.to_messages(rich_content=True, strict_role_orders=True)
    assert messages == [
        {'role': 'user', 'content': [{'type': 'text', 'text': '[CHAT HISTORY]'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Hi, how can I help you today?'}]},
        {'role': 'user', 'content': [{'type': 'text', 'text': '?'}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': '[User continue input]'}]},
        {'role': 'user', 'content': 'hi'},
    ]
    messages = prompt.to_messages(rich_content=False, strict_role_orders=True)
    assert messages == [
        {'role': 'user', 'content': '[CHAT HISTORY]'},
        {'role': 'assistant', 'content': 'Hi, how can I help you today?'},
        {'role': 'user', 'content': '?'},
        {'role': 'assistant', 'content': '[User continue input]'},
        {'role': 'user', 'content': 'hi'},
    ]
