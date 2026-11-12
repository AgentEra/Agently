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

from __future__ import annotations

from typing import Any, Literal
from typing_extensions import TypedDict


class RecordRef(TypedDict):
    id: str
    collection: str
    kind: str | None
    path: str | None
    sha256: str | None
    size: int
    summary: str
    scope: dict[str, Any]
    source: dict[str, Any]
    created_at: str
    meta: dict[str, Any]


class RecordLink(TypedDict):
    id: str
    source_id: str
    target_id: str
    relation: str
    created_at: str
    meta: dict[str, Any]


RecordStoreMode = Literal["read_only", "read_write"]


class SnapshotRetentionPolicy(TypedDict):
    keep_last: int | None


class SnapshotPruneResult(TypedDict):
    run_id: str
    keep_last: int
    retained_records: int
    deleted_records: int
    deleted_bytes: int


class RecordStoreCapabilities(TypedDict):
    root: str
    mode: RecordStoreMode
    external_read: bool
    external_write: bool
    private_write: bool
    materialized_components: list[str]


class RecordReference(TypedDict):
    record_store_id: str
    kind: str
    collection: str
    record_id: str
    version: str | None
    content_ref: str | None
    digest: str | None
    size: int
    created_at: str
    policy_labels: list[str]
    backend_capabilities: dict[str, bool]


class RecordContentSegment(TypedDict):
    ref: RecordReference
    content: str
    offset: int
    size: int
    total_size: int
    eof: bool
    digest: str | None
    content_type: str | None


class StoredRuntimeEvent(TypedDict):
    id: str
    execution_id: str
    sequence: int
    event_id: str
    event_type: str
    state_version: int | None
    idempotency_key: str | None
    parent_id: str | None
    causation_id: str | None
    parent_signal_id: str | None
    node_id: str | None
    operator_id: str | None
    interrupt_id: str | None
    resume_request_id: str | None
    actor_id: str | None
    lease_owner_id: str | None
    aggregation_scope: str | None
    snapshot_ref: RecordReference | None
    exchange_id: str | None
    artifact_refs: list[RecordReference]
    event: dict[str, Any]
    created_at: str
    persisted_at: str | None


class ExecutionLease(TypedDict):
    run_id: str
    owner_id: str
    lease_token: str
    lease_ttl: float
    lease_until: float
    claimed_at: str
    heartbeat_at: str
    released_at: str | None
    state_version: int | None


class RecordSearchResult(TypedDict, total=False):
    ref: RecordRef
    score: float | None
    reason: str | None


RecordRetrievalSource = Literal["record"]
RecordRetrievalSelection = Literal["length", "top_n"]
RecordRetrievalMethod = Literal["auto", "keyword", "vector", "hybrid"]


class RecordRetrievalItem(TypedDict, total=False):
    source: RecordRetrievalSource
    candidate_id: str
    ref: RecordRef
    kind: str | None
    summary: str
    content: str | None
    tags: list[str]
    score: float | None
    reason: str | None
    use: str
    chars: int
    body_state: str
    content_state: str
    original_ref: dict[str, Any]
    projection: dict[str, Any]
    raw_chars: int
    projected_chars: int
    truncated: bool


class RecordRetrievalOmission(TypedDict):
    reason: str
    count: int


class RecordRetrievalPackage(TypedDict):
    query: str | None
    profile: str
    selection: RecordRetrievalSelection
    items: list[RecordRetrievalItem]
    omitted: list[RecordRetrievalOmission]
    diagnostics: dict[str, Any]


class RerankScore(TypedDict):
    """One reranker relevance score keyed by the caller's document index.

    `score` carries the provider's own scale. Agently only compares scores
    within one response and never treats them as cross-model confidence.
    """

    index: int
    score: float
