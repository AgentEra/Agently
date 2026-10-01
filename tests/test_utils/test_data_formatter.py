
from agently.utils import DataFormatter


def test_sanitize_frozen_context_content_keeps_nested_values_and_source_immutable():
    import json
    from types import MappingProxyType
    from agently.types.data import ContextBlock

    block = ContextBlock(
        block_id="block", block_key="context", source_id="source",
        source_revision="1", source_ref="state", binding_id="binding",
        role="information", completeness="complete", content_chars=100,
        content={"taskboard": "- [ ] finish", "observations": [
            {"status": "success", "result": {"count": 3, "optional": None, "valid": True}}
        ]},
    )
    assert isinstance(block.content, MappingProxyType)
    result = DataFormatter.sanitize(block.content)
    assert isinstance(result, dict)
    assert json.loads(json.dumps(result)) == {
        "taskboard": "- [ ] finish", "observations": [
            {"status": "success", "result": {"count": 3, "optional": None, "valid": True}}
        ]}
    result["observations"][0]["result"]["count"] = 99
    assert block.content["observations"][0]["result"]["count"] == 3


def test_sanitize_mapping_preserves_declared_types_when_requested():
    from collections import UserDict
    from types import MappingProxyType

    value = UserDict({1: MappingProxyType({"value": (int, "quantity")})})
    assert DataFormatter.sanitize(value) == {"1": {"value": ("int", "quantity")}}
    assert DataFormatter.sanitize(value, remain_type=True) == {"1": {"value": (int, "quantity")}}


def test_sanitize():
    from pydantic import BaseModel, Field

    class MySubModel(BaseModel):
        "My Sub Model"

        dictionary: dict = Field(..., description="A Dictionary")
        some_list: list = Field(..., description="A List")

    class MyModel(BaseModel):
        "My Data Model"

        first: str = Field(..., description="Some String")
        second: int = Field(..., description="Some Integer")
        dict_list: list[dict[str, str]] = Field(..., description="A list of string dict")
        sub: MySubModel

    test_output = {
        "thinking": ([(str,)], "how to answer"),
        "reply": (str, "my reply"),
        "additional": MyModel,
    }
    sanitized = DataFormatter.sanitize(test_output)
    assert sanitized["thinking"] == ([("str",)], "how to answer")
    assert sanitized["reply"] == ("str", "my reply")
    assert sanitized["additional"]["first"] == ("str", "Some String")
    assert sanitized["additional"]["second"] == ("int", "Some Integer")
    assert sanitized["additional"]["dict_list"] in (
        ("list[dict[str, str]]", "A list of string dict"),
        ("list", "A list of string dict"),
    )
    assert sanitized["additional"]["sub"] in (
        ({"dictionary": ("dict", "A Dictionary"), "some_list": ("list", "A List")},),
        ({"dictionary": ("dict", "A Dictionary"), "some_list": ("list", "A List")}, "My Sub Model"),
    )

    sanitized_with_type = DataFormatter.sanitize(test_output, remain_type=True)
    assert sanitized_with_type["thinking"] == ([(str,)], "how to answer")
    assert sanitized_with_type["reply"] == (str, "my reply")
    assert sanitized_with_type["additional"]["first"] == (str, "Some String")
    assert sanitized_with_type["additional"]["second"] == (int, "Some Integer")
    assert sanitized_with_type["additional"]["dict_list"] in (
        (list[dict[str, str]], "A list of string dict"),
        (list, "A list of string dict"),
    )
    assert sanitized_with_type["additional"]["sub"] in (
        ({"dictionary": (dict, "A Dictionary"), "some_list": (list, "A List")},),
        ({"dictionary": (dict, "A Dictionary"), "some_list": (list, "A List")}, "My Sub Model"),
    )


def test_to_str_dict():
    test_value = {
        1: str,
        2: 3,
    }
    assert DataFormatter.to_str_key_dict(test_value) == {"1": str, "2": 3}
    assert DataFormatter.to_str_key_dict(
        test_value,
        value_format="serializable",
    ) == {
        "1": "str",
        "2": 3,
    }
    assert DataFormatter.to_str_key_dict(
        test_value,
        value_format="str",
    ) == {
        "1": "str",
        "2": "3",
    }


def test_sanitize_preserves_pydantic_generics():
    from pydantic import BaseModel, Field

    class GenericModel(BaseModel):
        dict_list: list[dict[str, str]] = Field(..., description="A list of string dict")

    sanitized = DataFormatter.sanitize(GenericModel)
    assert sanitized["dict_list"] in (
        ("list[dict[str, str]]", "A list of string dict"),
        ("list", "A list of string dict"),
    )

    sanitized_with_type = DataFormatter.sanitize(GenericModel, remain_type=True)
    assert sanitized_with_type["dict_list"] in (
        (list[dict[str, str]], "A list of string dict"),
        (list, "A list of string dict"),
    )


def test_from_schema_to_kwargs_format_additional_properties_false():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string", "title": "A"}},
        "additionalProperties": False,
    }
    assert DataFormatter.from_schema_to_kwargs_format(schema) == {"a": ("string", "")}


def test_from_schema_to_kwargs_format_additional_properties_true():
    from typing import Any

    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": True,
    }
    assert DataFormatter.from_schema_to_kwargs_format(schema) == {"a": ("string", ""), "<*>": (Any, "")}


def test_from_schema_to_kwargs_format_additional_properties_schema():
    schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": {"type": "number", "title": "X", "description": "x"},
    }
    assert DataFormatter.from_schema_to_kwargs_format(schema) == {"<*>": ("number", "description: x")}
