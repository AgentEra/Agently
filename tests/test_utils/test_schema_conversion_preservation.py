from copy import deepcopy
from types import SimpleNamespace

import pytest

from agently.utils import DataFormatter
from agently.utils.MCP import mcp_tool_schema


@pytest.mark.parametrize("kind", ["properties", "additionalProperties", "both"])
def test_schema_conversion_preserves_caller_schema_and_repeated_result(kind):
    schema = {"type": "object"}
    expected = {}
    if kind in ("properties", "both"):
        schema["properties"] = {
            "name": {"type": "string", "title": "Name", "description": "Display name"}
        }
        expected["name"] = ("string", "description: Display name")
    if kind in ("additionalProperties", "both"):
        schema["additionalProperties"] = {
            "type": "number", "title": "Score", "minimum": 0
        }
        expected["<*>"] = ("number", "minimum: 0")
    original = deepcopy(schema)

    assert DataFormatter.from_schema_to_kwargs_format(schema) == expected
    assert schema == original
    assert DataFormatter.from_schema_to_kwargs_format(schema) == expected


def test_shared_mcp_input_output_schema_keeps_both_descriptors_typed():
    schema = {
        "type": "object",
        "properties": {"value": {"type": "integer", "title": "Value"}},
        "additionalProperties": False,
    }
    tool = SimpleNamespace(input_schema=schema, output_schema=schema)
    original = deepcopy(schema)

    kwargs = DataFormatter.from_schema_to_kwargs_format(mcp_tool_schema(tool, "input"))
    returns = DataFormatter.from_schema_to_kwargs_format(mcp_tool_schema(tool, "output"))

    assert kwargs == returns == {"value": ("integer", "")}
    assert tool.input_schema == tool.output_schema == original
