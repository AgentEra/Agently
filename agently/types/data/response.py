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

from functools import lru_cache

from typing import TYPE_CHECKING, Any, AsyncGenerator, Awaitable, Callable, Literal, TypeAlias
from typing_extensions import TypedDict

from pydantic import BaseModel, model_validator

if TYPE_CHECKING:
    from agently.utils import GeneratorConsumer
    from agently.types.data.serializable import SerializableValue
    from agently.core import Prompt
    from agently.utils import Settings
    from agently.types.data.event import RunContext

AgentlyModelResultEvent = Literal[
    "error",
    "original_delta",
    "reasoning_delta",
    "delta",
    "tool_calls",
    "original_done",
    "reasoning_done",
    "done",
    "meta",
    "extra",
    "status",
]

AgentlyModelResultMessage: TypeAlias = tuple[AgentlyModelResultEvent, Any]
AgentlySpecificResultMessage: TypeAlias = AgentlyModelResultMessage
AgentlyOriginalResultPayload: TypeAlias = Any
AgentlyResultGenerator: TypeAlias = AsyncGenerator[AgentlyModelResultMessage, None]

AgentlyModelResponseEvent: TypeAlias = AgentlyModelResultEvent
AgentlyModelResponseMessage: TypeAlias = AgentlyModelResultMessage
AgentlySpecificResponseMessage: TypeAlias = AgentlySpecificResultMessage
AgentlyOriginalResponsePayload: TypeAlias = AgentlyOriginalResultPayload
AgentlyResponseGenerator: TypeAlias = AgentlyResultGenerator

NormalStreamingContentType: TypeAlias = Literal["delta", "original", "specific"]
InstantStreamingContentType: TypeAlias = Literal["instant", "streaming_parse"]
StreamingContentType: TypeAlias = NormalStreamingContentType | InstantStreamingContentType
ResultContentType: TypeAlias = Literal["all"] | StreamingContentType
ResponseContentType: TypeAlias = ResultContentType
SpecificEvents: TypeAlias = list[AgentlyModelResultEvent] | AgentlyModelResultEvent | None


class AgentlyModelResult(TypedDict):
    result_consumer: "GeneratorConsumer | None"
    meta: dict[str, Any]
    original_delta: list[dict[str, Any]]
    original_done: dict[str, Any]
    text_result: str
    cleaned_result: str | None
    parsed_result: "SerializableValue"
    result_object: BaseModel | None
    errors: list[Exception]
    extra: dict[str, Any]


OutputValidateResultDict = TypedDict(
    "OutputValidateResultDict",
    {
        "ok": bool,
        "reason": str | None,
        "payload": dict[str, Any] | None,
        "validator_name": str | None,
        "no_retry": bool | None,
        "stop": bool | None,
        "error": Exception | str | None,
        "exception": Exception | str | None,
        "raise": Exception | str | None,
    },
    total=False,
)


class OutputValidateContext:
    def __init__(
        self,
        *,
        value: dict[str, Any],
        agent_name: str,
        response_id: str,
        attempt_index: int,
        retry_count: int,
        max_retries: int,
        prompt: "Prompt",
        settings: "Settings",
        request_run_context: "RunContext | None",
        model_run_context: "RunContext | None",
        response_text: str,
        parsed_result: Any,
        result_object: BaseModel | None,
        meta: dict[str, Any] | None = None,
    ):
        self.value = value
        self.input = value
        self.agent_name = agent_name
        self.response_id = response_id
        self.attempt_index = attempt_index
        self.retry_count = retry_count
        self.max_retries = max_retries
        self.prompt = prompt
        self.settings = settings
        self.request_run_context = request_run_context
        self.model_run_context = model_run_context
        self.response_text = response_text
        self.raw_text = response_text
        self.parsed_result = parsed_result
        self.result_object = result_object
        self.typed = result_object
        self.meta = meta.copy() if isinstance(meta, dict) else {}

    def to_dict(self):
        return {
            "value": self.value,
            "input": self.input,
            "agent_name": self.agent_name,
            "response_id": self.response_id,
            "attempt_index": self.attempt_index,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "response_text": self.response_text,
            "parsed_result": self.parsed_result,
            "result_object": self.result_object,
            "meta": self.meta.copy(),
            "request_run_context": self.request_run_context,
            "model_run_context": self.model_run_context,
        }


OutputValidateResult: TypeAlias = bool | OutputValidateResultDict
OutputValidateHandler: TypeAlias = Callable[
    [dict[str, Any], OutputValidateContext],
    OutputValidateResult | Awaitable[OutputValidateResult],
]


class StreamingData(BaseModel):
    """
    Represents a streaming event for a specific path in a JSON structure.

    Attributes:
        path (str): The dot-style path to the field in the JSON object.
        value (Any): The current value at this path.
        delta (Optional[str]): The incremental content (for delta events, typically used for string updates).
        is_complete (bool): Whether this path/field is considered complete and will not change further.
        event_type (Literal["delta", "done"]): The type of event ("delta" for incremental update, "done" for completion).
    """

    path: str
    value: Any
    delta: str | None = None
    is_complete: bool = False
    wildcard_path: str | None = None
    indexes: tuple | None = None
    event_type: Literal["delta", "done"] = "done"
    full_data: Any = None
    completion_source: Literal[
        "observed_boundary",
        "final_reconciliation",
        "synthetic_repair",
    ] | None = None

    @staticmethod
    @lru_cache(maxsize=1024)
    def _process_path(path: str) -> tuple[str, tuple[int, ...]]:
        if '[' not in path:
            return path, ()
        wildcard_chars = []
        indexes: list[int] = []
        i = 0
        length = len(path)
        while i < length:
            c = path[i]
            if c == '[':
                j = i + 1
                num_start = j
                while j < length and path[j].isdigit():
                    j += 1
                if j > num_start and j < length and path[j] == ']':
                    num_str = path[num_start:j]
                    indexes.append(int(num_str))
                    wildcard_chars.append('[*]')
                    i = j + 1
                    continue
                else:
                    wildcard_chars.append(c)
                    i += 1
                    continue
            else:
                wildcard_chars.append(c)
                i += 1
        wildcard = ''.join(wildcard_chars)
        return wildcard, tuple(indexes)

    @model_validator(mode="before")
    @classmethod
    def set_wildcard_path(cls, data: dict[str, Any]):
        data["wildcard_path"], data["indexes"] = StreamingData._process_path(data["path"])
        return data


class AgentExecutionStreamData(StreamingData):
    """Process-stream item for an Agent execution route.

    The type keeps the same path/value/is_complete shape as model instant
    streams, then adds route metadata for Actions, Skills, Dynamic Task, and
    TriggerFlow-backed process events.
    """

    source: str | None = None
    route: str | None = None
    stage_id: str | None = None
    task_id: str | None = None
    action_id: str | None = None
    graph_id: str | None = None
    meta: dict[str, Any] | None = None


ModelStreamingHandler: TypeAlias = Callable[[StreamingData], Awaitable[None] | None]
AgentExecutionStreamHandler: TypeAlias = Callable[[AgentExecutionStreamData], Awaitable[None] | None]
