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

from collections.abc import Sequence
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from agently.types.data.event import RuntimeEvent, RuntimeEventDict
from agently.types.data.record_store import (
    RecordStoreCapabilities,
    RerankScore,
    RecordContentSegment,
    ExecutionLease,
    RecordLink,
    RecordRef,
    RecordReference,
    SnapshotPruneResult,
    SnapshotRetentionPolicy,
    StoredRuntimeEvent,
)


@runtime_checkable
class CheckpointStore(Protocol):
    async def put_checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> RecordRef: ...


@runtime_checkable
class DurableCheckpointStore(CheckpointStore, Protocol):
    async def get_checkpoint(self, run_id: str) -> RecordRef | None: ...

    async def latest_checkpoint(self, run_id: str) -> RecordRef | None: ...

    async def checkpoint_history(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
        limit: int | None = None,
    ) -> list[RecordRef]: ...

    async def claim_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl: float,
        expected_state_version: int | None = None,
    ) -> ExecutionLease: ...

    async def heartbeat_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> ExecutionLease: ...

    async def release_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> ExecutionLease: ...


@runtime_checkable
class ExecutionSnapshotStore(Protocol):
    async def put_snapshot(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> RecordRef: ...

    async def get_snapshot(self, run_id: str) -> dict[str, Any] | None: ...

    async def latest_snapshot(self, run_id: str) -> RecordRef | None: ...

@runtime_checkable
class ExecutionSnapshotRetentionStore(ExecutionSnapshotStore, Protocol):
    async def put_snapshot(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
        retention: SnapshotRetentionPolicy | None = None,
    ) -> RecordRef: ...

    async def prune_snapshots(
        self,
        run_id: str,
        *,
        keep_last: int,
    ) -> SnapshotPruneResult: ...

    async def delete_snapshot(self, run_id: str) -> dict[str, Any]: ...


@runtime_checkable
class RuntimeEventStore(Protocol):
    """Explicit audit sink; binding a RecordStore does not activate this port."""

    async def append_runtime_event(
        self,
        execution_id: str,
        event: RuntimeEvent | RuntimeEventDict | dict[str, Any],
        *,
        sequence: int | None = None,
        expected_sequence: int | None = None,
        idempotency_key: str | None = None,
        snapshot_ref: RecordRef | RecordReference | str | None = None,
        artifact_refs: list[RecordRef | RecordReference | str] | None = None,
        exchange_id: str | None = None,
        state_version: int | None = None,
        parent_id: str | None = None,
        causation_id: str | None = None,
        parent_signal_id: str | None = None,
        node_id: str | None = None,
        operator_id: str | None = None,
        interrupt_id: str | None = None,
        resume_request_id: str | None = None,
        actor_id: str | None = None,
        lease_owner_id: str | None = None,
        aggregation_scope: str | None = None,
    ) -> StoredRuntimeEvent: ...

    async def query_runtime_events(
        self,
        execution_id: str,
        *,
        sequence_from: int | None = None,
        sequence_to: int | None = None,
        event_id: str | None = None,
        limit: int | None = None,
    ) -> list[StoredRuntimeEvent]: ...


@runtime_checkable
class RefResolver(Protocol):
    async def ref_envelope(
        self, ref_or_id: RecordRef | str
    ) -> RecordReference: ...

    async def read_bounded(
        self,
        ref_or_path: RecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> RecordContentSegment: ...

    def stream_read(
        self,
        ref_or_path: RecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[RecordContentSegment]: ...


@runtime_checkable
class EvidenceLinker(Protocol):
    async def link_evidence(
        self,
        source: RecordRef | str,
        target: RecordRef | str,
        relation: str,
        *,
        execution_id: str | None = None,
        operation_id: str | None = None,
        runtime_event_id: str | None = None,
        checkpoint_id: str | None = None,
        exchange_id: str | None = None,
        artifact_refs: list[RecordRef | RecordReference | str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> RecordLink: ...


@runtime_checkable
class TextIndex(Protocol):
    async def index_record(self, ref: RecordRef, content: str) -> None: ...

    async def search(
        self,
        query: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[RecordRef]: ...


@runtime_checkable
class VectorIndex(Protocol):
    async def index_record(self, ref: RecordRef, content: str) -> None: ...

    async def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[RecordRef]: ...


@runtime_checkable
class DBStoreProvider(Protocol):
    """Optional record/recovery/audit database port, activated on first use."""

    name: str

    async def put_record(self, ref: RecordRef) -> RecordRef: ...

    async def get_record(self, record_id: str) -> RecordRef | None: ...

    async def index_record(self, ref: RecordRef, content: str) -> None: ...

    async def search(
        self,
        query: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[RecordRef]: ...

    async def link(
        self,
        source: RecordRef | str,
        target: RecordRef | str,
        relation: str,
        meta: dict[str, Any] | None = None,
    ) -> RecordLink: ...

    async def links(
        self,
        ref_or_id: RecordRef | str | None = None,
        *,
        source: RecordRef | str | None = None,
        target: RecordRef | str | None = None,
        relation: str | None = None,
    ) -> list[RecordLink]: ...

    async def link_evidence(
        self,
        source: RecordRef | str,
        target: RecordRef | str,
        relation: str,
        **kwargs: Any,
    ) -> RecordLink: ...

    async def checkpoint(
        self, run_id: str, state: dict[str, Any], *, step_id: str | None = None
    ) -> RecordRef: ...

    async def put_checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> RecordRef: ...

    async def get_checkpoint(self, run_id: str) -> RecordRef | None: ...

    async def put_artifact_ref(
        self,
        run_id: str,
        artifact: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> RecordRef: ...

    async def claim_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl: float,
        expected_state_version: int | None = None,
    ) -> ExecutionLease: ...

    async def heartbeat_lease(
        self, run_id: str, owner_id: str, lease_token: str
    ) -> ExecutionLease: ...

    async def release_lease(
        self, run_id: str, owner_id: str, lease_token: str
    ) -> ExecutionLease: ...

    async def put_snapshot(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
        retention: SnapshotRetentionPolicy | None = None,
    ) -> RecordRef: ...

    async def get_snapshot(self, run_id: str) -> dict[str, Any] | None: ...

    async def latest_snapshot(self, run_id: str) -> RecordRef | None: ...

    async def delete_snapshot(self, run_id: str) -> dict[str, Any]: ...

    async def prune_snapshots(
        self,
        run_id: str,
        *,
        keep_last: int,
    ) -> SnapshotPruneResult: ...

    async def latest_checkpoint(self, run_id: str) -> RecordRef | None: ...

    async def checkpoint_history(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
        limit: int | None = None,
    ) -> list[RecordRef]: ...

    async def append_runtime_event(
        self,
        execution_id: str,
        event: RuntimeEvent | RuntimeEventDict | dict[str, Any],
        **kwargs: Any,
    ) -> StoredRuntimeEvent: ...

    async def query_runtime_events(
        self,
        execution_id: str,
        *,
        sequence_from: int | None = None,
        sequence_to: int | None = None,
        event_id: str | None = None,
        limit: int | None = None,
    ) -> list[StoredRuntimeEvent]: ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str

    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class RerankProvider(Protocol):
    """Optional relevance-scoring seam for retrieval rerank.

    Owns scoring only. Candidate construction, drop policy, refill, packaging
    and semantic acceptance stay with the retrieval layer. Returned scores use
    the backing model's own scale and are compared within one response only.
    """

    name: str

    async def rank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
    ) -> list[RerankScore]: ...


@runtime_checkable
class VectorStoreProvider(Protocol):
    name: str

    async def index_record(
        self, ref: RecordRef, embedding: list[float]
    ) -> None: ...

    async def search_by_embedding(
        self,
        embedding: list[float],
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[RecordRef]: ...

    async def delete_records(self, record_ids: Sequence[str]) -> None: ...


@runtime_checkable
class RecordStoreProviderFactory(Protocol):
    def __call__(
        self,
        *,
        root: Any | None = None,
        mode: str = "read_write",
        **options: Any,
    ) -> Any: ...


@runtime_checkable
class IngestionProfile(Protocol):
    name: str

    async def ingest(
        self,
        *,
        record_store: Any,
        content: Any,
        collection: str,
        kind: str | None,
        scope: dict[str, Any],
        source: dict[str, Any],
        summary: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> RecordRef: ...


@runtime_checkable
class RecordStoreBackend(Protocol):
    """Minimum full-backend replacement contract.

    Recovery, audit, retrieval and indexing capabilities are separate optional
    ports and are detected only when their operation is requested.
    """

    @property
    def root(self) -> Any: ...

    async def put(
        self,
        content: Any,
        *,
        collection: str,
        kind: str | None = None,
        summary: str | None = None,
        scope: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        indexed: bool = False,
        vector: bool = False,
    ) -> RecordRef: ...

    async def get_data(self, ref_or_path: RecordRef | str) -> Any: ...

    async def search(
        self,
        query: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[RecordRef]: ...

    def capabilities(self) -> RecordStoreCapabilities: ...


@runtime_checkable
class RecordStoreBackendProvider(Protocol):
    def __call__(
        self,
        *,
        root: Any | None = None,
        create: bool = True,
        mode: str = "read_write",
        **options: Any,
    ) -> RecordStoreBackend: ...


__all__ = [
    "CheckpointStore",
    "DBStoreProvider",
    "DurableCheckpointStore",
    "EmbeddingProvider",
    "EvidenceLinker",
    "ExecutionSnapshotStore",
    "ExecutionSnapshotRetentionStore",
    "IngestionProfile",
    "RefResolver",
    "RerankProvider",
    "RuntimeEventStore",
    "TextIndex",
    "VectorIndex",
    "VectorStoreProvider",
    "RecordStoreBackend",
    "RecordStoreBackendProvider",
    "RecordStoreProviderFactory",
]
