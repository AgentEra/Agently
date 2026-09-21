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

from .base import print_, async_print, AgentlyMain, Agent
from ._version import __version__
from .types.data.judgment import OutputTemplate, Probability, Choice, Score
from .core.model.AudioModelRequest import AudioModelRequest
from .types.data.audio import (
    AudioCapabilityError, AudioConnection, AudioFormat, AudioInput, AudioInputOptions, AudioInputEvent, AudioInputHandler, AudioOperation, AudioProtocolError,
    PCMFormat, SpeechOptions, SpeechRequest, SpeechResult, TranscriptEvent, TranscriptResult,
    PCMStream, TextSource, TextSegmentOptions, TranscriptionStreamOptions, TranscriptBlock, TranscriptSegment,
    TranscriptionOptions, TranscriptionRequest,
)
from .types.plugins.AudioModelRequester import AudioCapability, AudioModelRequester, TextSegmenter, SpeechDetector, SpeechDetectionSession
from .core import (
    AgentTask,
    TaskContext,
    TaskWorkspace,
    TriggerFlow,
    TriggerFlowBlueprint,
)
from .types.data import (
    LongContent,
    AgentExecutionStreamData,
    AgentExecutionStreamHandler,
    AgentlyModelResultEvent,
    AgentlyModelResultMessage,
    AgentlyOriginalResultPayload,
    AgentlySpecificResultMessage,
    AgentlyResultGenerator,
    EventHook,
    ModelStreamingHandler,
    ObservationEvent,
    ObservationEventHook,
    RuntimeEvent,
    RuntimeEventHook,
    ResultContentType,
    SpecificEvents,
    SkillRuntimeStreamHandler,
    SkillRuntimeStreamItem,
    StreamingData,
)
from .types.trigger_flow import (
    TriggerFlowContractSpec,
    TriggerFlowEventData,
    TriggerFlowIntervention,
    TriggerFlowInterventionEvent,
    TriggerFlowInterruptEvent,
    TriggerFlowRuntimeData,
    TriggerFlowSystemStreamEvent,
)

Agently = AgentlyMain()

__all__ = [
    "OutputTemplate",
    "Probability",
    "Choice",
    "Score",
    "LongContent",
    "Agently",
    "__version__",
    "Agent",
    "AgentTask",
    "TaskContext",
    "TaskWorkspace",
    "TriggerFlow",
    "TriggerFlowContractSpec",
    "TriggerFlowRuntimeData",
    "TriggerFlowEventData",
    "TriggerFlowIntervention",
    "TriggerFlowInterventionEvent",
    "TriggerFlowInterruptEvent",
    "TriggerFlowSystemStreamEvent",
    "TriggerFlowBlueprint",
    "StreamingData",
    "AgentExecutionStreamData",
    "AgentlyModelResultEvent",
    "AgentlyModelResultMessage",
    "AgentlySpecificResultMessage",
    "AgentlyOriginalResultPayload",
    "AgentlyResultGenerator",
    "ResultContentType",
    "SpecificEvents",
    "ModelStreamingHandler",
    "AgentExecutionStreamHandler",
    "SkillRuntimeStreamItem",
    "SkillRuntimeStreamHandler",
    "RuntimeEvent",
    "ObservationEvent",
    "EventHook",
    "RuntimeEventHook",
    "ObservationEventHook",
    "print_",
    "async_print",
    "AudioModelRequest",
    "AudioModelRequester",
    "AudioCapability",
    "AudioCapabilityError",
    "AudioConnection",
    "AudioFormat",
    "AudioInput",
    "AudioInputOptions",
    "AudioInputEvent",
    "AudioInputHandler",
    "SpeechDetector",
    "SpeechDetectionSession",
    "AudioOperation",
    "AudioProtocolError",
    "PCMFormat",
    "PCMStream",
    "TextSource",
    "TextSegmentOptions",
    "TextSegmenter",
    "TranscriptionStreamOptions",
    "TranscriptBlock",
    "TranscriptSegment",
    "SpeechOptions",
    "SpeechRequest",
    "SpeechResult",
    "TranscriptEvent",
    "TranscriptResult",
    "TranscriptionOptions",
    "TranscriptionRequest",
]
