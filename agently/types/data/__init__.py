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


class AVOID_COPY:
    __slots__ = ("id",)

    def __init__(self):
        import uuid

        self.id = uuid.uuid4().hex

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        return (self.__class__, (), {"id": self.id})


EMPTY = AVOID_COPY()

from .output import LongContent
from .shell import (
    ShellLanguage as ShellLanguage, ShellEnvironment as ShellEnvironment,
    ShellApproval as ShellApproval, ShellEffect as ShellEffect,
    ShellRisk as ShellRisk, ShellRiskHandler as ShellRiskHandler, ShellResult as ShellResult,
)

from .context import (
    ContextBlock,
    ContextBudget,
    ContextCandidate,
    ContextCompleteness,
    ContextConsumer,
    ContextConsumption,
    ContextDiagnostic,
    ContextOmission,
    ContextPackage,
    ContextPhase,
    ContextReadIntent,
    ContextRole,
    ContextSourceBindingSnapshot,
    ContextSourceChange,
    ContextSourceChangeSet,
    ContextSourceDescriptor,
    ContextSourceDescriptorPage,
    ContextSourceRead,
    TaskContextEntrySnapshot,
    TaskContextSnapshot,
)
from .task_workspace import (
    TaskWorkspaceAccessGrant,
    TaskWorkspaceAccessMode,
    TaskWorkspaceAccessRequirement,
    TaskWorkspaceAccessRoot,
    TaskWorkspaceAccessRootRole,
    TaskWorkspaceDiagnostic,
    TaskWorkspaceExportResult,
    TaskWorkspaceFileInfo,
    TaskWorkspaceFileOperation,
    TaskWorkspaceFileRead,
    TaskWorkspaceFileRef,
    TaskWorkspaceFileWrite,
    TaskWorkspaceExecutionManifest,
    TaskWorkspaceExecutionManifestFile,
    TaskWorkspaceReadResult,
    TaskWorkspaceRetentionDiagnostic,
    TaskWorkspaceRetentionResult,
    TaskWorkspaceTerminalStatus,
    TaskWorkspaceWriteResult,
)
from .serializable import SerializableData, SerializableMapping as SerializableMapping, SerializableValue as SerializableValue
from .prompt import (
    ChatMessage,
    ChatMessageDict,
    ChatMessageContent,
    TextMessageContent,
    PromptModel,
    PromptOutputStructure,
    PromptStandardSlot,
    ToolMeta,
)
from .request import (
    APIKeyFailoverAction,
    APIKeyFailoverContext,
    APIKeySelectionContext,
    AgentlyRequestData as AgentlyRequestData,
    AgentlyRequestDataDict,
    ModelProfileResolution,
)

from .response import (
    AgentExecutionStreamData,
    AgentExecutionStreamHandler,
    AgentlyOriginalResultPayload,
    AgentlyOriginalResponsePayload,
    AgentlyModelResult,
    AgentlyModelResultEvent,
    AgentlyModelResultMessage,
    AgentlyModelResponseEvent,
    AgentlyModelResponseMessage,
    AgentlySpecificResultMessage,
    AgentlySpecificResponseMessage,
    AgentlyResultGenerator,
    AgentlyResponseGenerator,
    InstantStreamingContentType,
    ModelStreamingHandler,
    NormalStreamingContentType,
    OutputValidateContext,
    OutputValidateHandler,
    OutputValidateResult,
    OutputValidateResultDict,
    ResultContentType,
    ResponseContentType,
    SpecificEvents,
    StreamingContentType,
    StreamingData,
)

from .event import (
    ObservationEventLevel,
    RuntimeEventLevel,
    EventDeliveryMode,
    EventDispatchMode,
    EventDeliveryPolicy,
    RunKind,
    ErrorInfoDict,
    RunContextDict,
    ObservationEventDict,
    RuntimeEventDict,
    ErrorInfo,
    RunContext,
    ObservationEvent,
    RuntimeEvent,
    RuntimeEventHook,
    EventHook,
    ObservationEventHook,
)

from .tool import (
    ArgumentDesc,
    KwargsType,
    ReturnType,
    MCPConfig,
    MCPConfigs,
    ToolInfo,
)

from .task_dag import (
    TASK_DAG_SCHEMA_VERSION,
    TaskDAG,
    TaskDAGNode,
)

from .task_board import (
    TASK_BOARD_SCHEMA_VERSION,
    TaskBoardCard,
    TaskBoardCardFailurePolicy,
    TaskBoardCardResult,
    TaskBoardCardStatus,
    TaskBoardGraph,
    TaskBoardPatch,
    TaskBoardRevision,
    TaskBoardSchedulePlan,
    TaskBoardStatus,
)

from .action import (
    PROGRAMMATIC_ACTION_ARTIFACT_READ_ID,
    PROGRAMMATIC_ACTION_SDK_RENDERER_VERSION,
    PROGRAMMATIC_ACTION_TRANSPORT_ID,
    ActionApproval,
    ActionArtifact,
    ActionCall,
    ActionDecision,
    ActionDiagnostic,
    ActionPolicy,
    ActionPlanningRequest,
    ActionPlanningObservation as ActionPlanningObservation,
    ActionExecutionRequest,
    ActionResult,
    ActionRunContext,
    ActionPlanningProtocol,
    ActionConcurrencyMode,
    ActionSideEffectLevel,
    ActionSpec,
    ActionStatus,
    ProgrammaticActionCatalog as ProgrammaticActionCatalog,
    ProgrammaticActionCatalogEntry,
    ProgrammaticActionDecision,
    ProgrammaticActionObservation as ProgrammaticActionObservation,
)

from .capability import (
    CapabilityRiskLevel,
    CapabilitySideEffectDescriptor,
    CapabilitySideEffectOwner,
)

from .execution_resource import (
    ExecutionResourceDecision,
    ExecutionResourceHandle,
    ExecutionResourceKind,
    ExecutionResourcePolicy,
    ExecutionResourceProviderCandidate,
    ExecutionResourceProviderProbe,
    ExecutionResourceRequirement,
    ExecutionResourceScope,
    ExecutionResourceStatus,
)

from .code_execution import (
    CodeExecutionBinding,
    CodeExecutionBindingConcurrencyMode,
    CodeExecutionBindingCallRecord,
    CodeExecutionBindingError,
    CodeExecutionBindingLimits,
    CodeExecutionBindingStatus,
    CodeExecutionBindingSummary,
    CodeExecutionSchemaValidationError,
    CodeExecutionBundle,
    CodeExecutionFile,
    CodeExecutionFileRole,
    CodeExecutionIsolationCapability,
    CodeExecutionProviderCapability,
    CodeExecutionRequest,
    CodeExecutionResult,
    CodeExecutionStep,
    CodeExecutionStepRole,
    CodeExecutionToolchainRequirement,
    code_execution_json_bytes,
    normalize_code_execution_json_value,
    required_code_execution_isolation,
    resolve_code_execution_workspace_uri,
    validate_code_execution_json_schema_definition,
    validate_code_execution_json_schema,
)

from .execution_plan import (
    PREFERRED_EXECUTION_SHAPES,
    PLAN_BLOCK_INSTANCE_KINDS,
    EVIDENCE_BODY_STATES,
    EVIDENCE_ITEM_STATUSES,
    EXECUTION_PLAN_SCHEMA_VERSION,
    REPLAN_STATUSES,
    CapabilityResolution,
    EvidenceEnvelope,
    EvidenceBodyState,
    EvidenceItemStatus,
    ExecutionPlanEdge,
    ExecutionPlan,
    PlanBlockInstance,
    PlanBlockInstanceKind,
    PreferredExecutionShape,
    ReplanSignal,
    ReplanStatus,
    TaskFrame,
)

from .blocks import (
    BLOCKS_SCHEMA_VERSION,
    BLOCK_COMPOSITIONS,
    EXECUTION_BLOCK_KINDS,
    PLAN_BLOCK_KINDS,
    STANDARD_BLOCK_SIGNALS,
    BlockCompileRequest,
    BlockComposition,
    BlockSignal,
    EvidenceMapper,
    ExecutionBlock,
    ExecutionBlockEdge,
    ExecutionBlockGraph,
    ExecutionBlockKind,
    PlanBlock,
    PlanBlockKind,
    ResultAdapter,
    StandardBlockSignal,
)

from .execution_exchange import (
    AgentInteractionHandler,
    ExchangeRoutingHandler,
    ExecutionExchangeDispatchState,
    ExecutionExchangeKind,
    ExecutionExchangeProviderResult,
    ExecutionExchangeRequest,
    ExecutionExchangeResponse,
    ExecutionExchangeRouting,
    ExecutionExchangeStatus,
    ExecutionExchangeView,
    ExecutionExchangeWaitMode,
)

from .policy_approval import (
    PolicyApprovalDecision,
    PolicyApprovalHandler,
    PolicyApprovalRequest,
    PolicyApprovalSource,
    PolicyApprovalStatus,
)

from .skill import (
    SkillMode,
    SkillRuntimeStreamHandler,
    SkillRuntimeStreamItem,
    SkillSourceRequest,
    SkillSourceSnapshot,
    SkillScriptAuthorization as SkillScriptAuthorization,
    redact_skill_source,
)

from .record_store import (
    RecordStoreCapabilities,
    RecordStoreMode,
    SnapshotPruneResult,
    SnapshotRetentionPolicy,
    RecordContentSegment,
    RecordRetrievalItem,
    RecordRetrievalMethod,
    RecordRetrievalOmission,
    RecordRetrievalPackage,
    RecordRetrievalSelection,
    RecordRetrievalSource,
    ExecutionLease,
    RecordLink,
    RecordRef,
    RecordReference,
    StoredRuntimeEvent,
    RecordSearchResult,
)

from .agent_execution import (
    AgentExecutionControlResult as AgentExecutionControlResult,
    AgentExecutionControlCapabilities as AgentExecutionControlCapabilities,
    AgentExecutionName,
    ActionArtifactReleaseDiagnostic,
    ActionArtifactReleaseDiagnostics,
    AgentExecutionDiagnostics,
    AgentExecutionEffort as AgentExecutionEffort,
    AgentExecutionLineage,
    AgentExecutionLimits,
    AgentExecutionMeta,
    AgentExecutionStatus,
    AgentExecutionStreamMeta,
    AgentExecutionRecordPurpose,
    AgentExecutionRecordWrite,
    AgentExecutionRecordRefs,
    AgentExecutionStrategy as AgentExecutionStrategy,
    CapabilityKind,
    CapabilityRoute,
    EvidenceRequirement,
    EvidenceRequirementKind,
    GuidanceAccess,
    PlannerCapabilityCandidate,
    PlannerCapabilitySummary,
)

from .agent_review import (
    AgentReviewQuality as AgentReviewQuality,
    AgentReviewFailureAction as AgentReviewFailureAction,
    AgentReviewIssue as AgentReviewIssue,
    AgentReviewCheck as AgentReviewCheck,
    AgentReviewContext as AgentReviewContext,
    AgentReviewHandler as AgentReviewHandler,
    AgentReviewHandlerResult as AgentReviewHandlerResult,
    AgentReviewResult as AgentReviewResult,
)

from .agent_artifact import (
    AgentArtifactContext as AgentArtifactContext,
    AgentArtifactHandler as AgentArtifactHandler,
    AgentArtifactHandlerResult as AgentArtifactHandlerResult,
    AgentArtifactResult as AgentArtifactResult,
)

from .attempt import (
    AttemptDecision,
    AttemptDecisionAction,
    AttemptHandlers,
    AttemptObservation,
    AttemptState,
    AttemptStreamGenerator,
    AttemptStreamMessage,
)
