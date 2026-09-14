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

import asyncio
import copy
import hashlib
import inspect
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager, asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Concatenate, ParamSpec, TypeVar, cast

from agently.types.data.event import RuntimeEvent, RuntimeEventDict
from agently.types.data.record_store import (
    RecordStoreCapabilities,
    RecordContentSegment,
    ExecutionLease,
    RecordLink,
    RecordRef,
    RecordReference,
    RecordRetrievalMethod,
    RecordRetrievalPackage,
    RecordRetrievalSelection,
    SnapshotPruneResult,
    SnapshotRetentionPolicy,
    StoredRuntimeEvent,
)
from agently.types.plugins import RecordStoreBackend
from .Retrieval import RerankHandler, retrieve_records
from .SnapshotRetention import normalize_snapshot_retention
from ._defaults import default_record_store_root, merge_scope

if TYPE_CHECKING:
    from .Registry import RecordStoreRegistry


_MISSING = object()
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _guard_record_store_mutation(
    method: Callable[Concatenate["RecordStore", _P], Coroutine[Any, Any, _R]],
) -> Callable[Concatenate["RecordStore", _P], Coroutine[Any, Any, _R]]:
    @wraps(method)
    async def guarded(
        self: "RecordStore",
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _R:
        async with self._backend_mutation_guard():
            return await method(self, *args, **kwargs)

    return cast(
        Callable[Concatenate["RecordStore", _P], Coroutine[Any, Any, _R]],
        guarded,
    )


class RecordStore:
    """RecordStore API bound to one backend."""

    def __init__(
        self,
        backend: RecordStoreBackend | str | Path | None = None,
        manager: "RecordStoreRegistry | None" = None,
        *,
        create: bool = True,
        mode: str = "read_only",
        provider: str | None = None,
        provider_options: dict[str, Any] | None = None,
        db_store_provider: Any | None = None,
        db_store_options: dict[str, Any] | None = None,
        embedding_provider: Any | None = None,
        embedding_options: dict[str, Any] | None = None,
        rerank_provider: Any | None = None,
        rerank_options: dict[str, Any] | None = None,
        vector_store_provider: Any | None = None,
        vector_store_options: dict[str, Any] | None = None,
        default_scope: dict[str, Any] | None = None,
        default_search_scope: dict[str, Any] | None = None,
        snapshot_retention: SnapshotRetentionPolicy | None = None,
    ):
        if mode not in {"read_only", "read_write"}:
            raise ValueError("RecordStore mode must be 'read_only' or 'read_write'.")
        if manager is None:
            from .Registry import RecordStoreRegistry

            manager = RecordStoreRegistry()
        self.manager = manager
        self._create = bool(create)
        self._provider = provider
        self._provider_options = dict(provider_options or {})
        self._db_store_provider = db_store_provider
        self._db_store_options = dict(db_store_options or {})
        self._embedding_provider = embedding_provider
        self._embedding_options = dict(embedding_options or {})
        self._rerank_provider = rerank_provider
        self._rerank_options = dict(rerank_options or {})
        self._vector_store_provider = vector_store_provider
        self._vector_store_options = dict(vector_store_options or {})
        self._snapshot_retention = (
            normalize_snapshot_retention(
                snapshot_retention,
                default_keep_last=3,
            )
            if snapshot_retention is not None
            else None
        )
        self._file_mutation_lock = asyncio.Lock()
        self._execution_id = uuid.uuid4().hex
        if backend is None or isinstance(backend, (str, Path)):
            self._backend: RecordStoreBackend | None = None
            selected_root = default_record_store_root() if backend is None else Path(backend)
            self.root = selected_root.expanduser().resolve()
            self.mode = mode
        else:
            self._backend = cast(RecordStoreBackend, backend)
            self.root = Path(str(getattr(self._backend, "root"))).expanduser().resolve()
            self.mode = "read_only" if bool(getattr(self._backend, "read_only", False)) else mode
        self._record_store_id = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()
        self.default_scope = dict(default_scope or {})
        self.default_search_scope = dict(default_search_scope or self.default_scope)

    @property
    def record_store_id(self) -> str:
        return self._record_store_id

    @property
    def execution_id(self) -> str:
        return self._execution_id

    def _bind_execution(
        self,
        execution_id: str,
        *,
        scope: dict[str, Any] | None = None,
        search_scope: dict[str, Any] | None = None,
    ) -> "RecordStore":
        if not str(execution_id).strip():
            raise ValueError("RecordStore execution_id cannot be empty.")
        # Execution binding is a view, not a new storage/provider binding. A
        # shallow copy preserves the caller's backend and instance-level
        # adapters while keeping execution identity and scopes view-local.
        bound = copy.copy(self)
        bound.default_scope = merge_scope(self.default_scope, scope)
        bound.default_search_scope = merge_scope(self.default_search_scope, search_scope)
        bound._execution_id = str(execution_id)
        return bound

    @property
    def backend(self) -> Any:
        if self._backend is None:
            materialization_root = (
                self.root
                if self._provider is not None
                else self.root / ".agently" / "records"
            )
            materialized = self.manager._materialize_record_store(
                materialization_root,
                create=self._create,
                mode="read_write",
                provider=self._provider,
                provider_options=self._provider_options,
                db_store_provider=self._db_store_provider,
                db_store_options=self._db_store_options,
                embedding_provider=self._embedding_provider,
                embedding_options=self._embedding_options,
                rerank_provider=self._rerank_provider,
                rerank_options=self._rerank_options,
                vector_store_provider=self._vector_store_provider,
                vector_store_options=self._vector_store_options,
                default_scope=self.default_scope,
                default_search_scope=self.default_search_scope,
            )
            self._backend = materialized._backend
        if self._backend is None:
            raise RuntimeError("RecordStore backend materialization did not produce a backend.")
        return self._backend

    @asynccontextmanager
    async def _backend_mutation_guard(self) -> AsyncIterator[None]:
        guard = getattr(self._backend, "_mutation_guard", None) if self._backend is not None else None
        if callable(guard):
            typed_guard = cast(
                Callable[[], AbstractAsyncContextManager[None]],
                guard,
            )
            async with typed_guard():
                yield
            return
        async with self._file_mutation_lock:
            yield

    def _try_backend_sync_mutation_guard(self) -> AbstractContextManager[bool] | None:
        guard = getattr(self._backend, "_try_sync_mutation_guard", None) if self._backend is not None else None
        if not callable(guard):
            return None
        typed_guard = cast(
            Callable[[], AbstractContextManager[bool]],
            guard,
        )
        return typed_guard()

    def _scoped_record_scope(self, scope: dict[str, Any] | None) -> dict[str, Any]:
        return merge_scope(self.default_scope, scope)

    def _scoped_filters(self, filters: dict[str, Any] | None) -> dict[str, Any]:
        scoped = dict(filters or {})
        for key, value in self.default_search_scope.items():
            filter_key = f"scope.{key}"
            scoped.setdefault(filter_key, value)
        return scoped

    def _context_snapshot(
        self,
        *,
        page: tuple[int, int] | None = None,
        exact: tuple[str, int, int] | None = None,
        projection_limit: int = 2000,
    ) -> tuple[str, tuple[RecordRef, ...], dict[str, RecordContentSegment]] | None:
        """Use only the original built-in read path; preserve provider/instance adapters."""
        from .LocalRecordStore import LocalRecordStore
        from .Registry import RecordStoreRegistry

        if type(self) is not RecordStore or self._provider is not None:
            return None
        if any(name in vars(self) for name in ("search", "read_bounded", "get_data", "_scoped_filters")):
            return None
        backend = self._backend
        if backend is None:
            if type(self.manager) is not RecordStoreRegistry or "_materialize_record_store" in vars(self.manager):
                return None
            # Pure path access: leave the facade binding and all component loaders lazy.
            backend = LocalRecordStore(self.root / ".agently" / "records", create=False, mode="read_only")
        if type(backend) is not LocalRecordStore:
            return None
        if any(name in vars(backend) for name in (
            "search", "read_bounded", "get_data", "get", "get_record", "_record_id",
            "_connect", "_table_exists", "_row_to_ref", "_matches_filters",
            "_decode_content", "_content_text", "ref_envelope", "_ref_envelope",
        )):
            return None
        return backend._context_snapshot(
            self._scoped_filters(None),
            record_store_id=self.record_store_id,
            page=page,
            exact=exact,
            projection_limit=projection_limit,
        )

    def _matches_default_search_scope(self, ref: RecordRef) -> bool:
        if not self.default_search_scope:
            return True
        scope = ref.get("scope")
        if not isinstance(scope, dict):
            return False
        for key, value in self.default_search_scope.items():
            if value is None:
                continue
            if scope.get(key) != value:
                return False
        return True

    async def _scope_record_ref(self, ref: RecordRef) -> RecordRef:
        if not self.default_scope:
            return ref
        scoped_ref = dict(ref)
        existing_scope = scoped_ref.get("scope")
        scoped_ref["scope"] = merge_scope(
            self.default_scope, existing_scope if isinstance(existing_scope, dict) else {}
        )
        put_record = getattr(self.backend, "put_record", None)
        if not callable(put_record):
            return cast(RecordRef, scoped_ref)
        put_record_callable = cast(Callable[[RecordRef], Awaitable[RecordRef]], put_record)
        return await put_record_callable(cast(RecordRef, scoped_ref))

    async def put(
        self,
        record_or_content: Any = _MISSING,
        *,
        content: Any = _MISSING,
        collection: str,
        kind: str | None = None,
        meta: dict[str, Any] | None = None,
        profile: str | None = None,
        indexed: bool = False,
        vector: bool = False,
        **kwargs,
    ):
        if record_or_content is _MISSING:
            if content is _MISSING:
                raise TypeError("RecordStore.put(...) requires record_or_content or content.")
            record_or_content = content
        elif content is not _MISSING:
            raise TypeError("RecordStore.put(...) accepts either record_or_content or content, not both.")
        profile_name = str(profile or "").strip()
        if profile_name:
            handler = self.manager.get_profile(profile_name)
            scope_value = kwargs.get("scope")
            source_value = kwargs.get("source")
            summary_value = kwargs.get("summary")
            return await handler.ingest(
                record_store=self,
                content=record_or_content,
                collection=collection,
                kind=kind,
                scope=self._scoped_record_scope(scope_value if isinstance(scope_value, dict) else None),
                source=source_value if isinstance(source_value, dict) else {},
                summary=(
                    summary_value if isinstance(summary_value, str) or summary_value is None else str(summary_value)
                ),
                meta=meta,
            )
        if self.default_scope:
            kwargs["scope"] = self._scoped_record_scope(kwargs.get("scope"))
        return await self.backend.put(
            record_or_content,
            collection=collection,
            kind=kind,
            meta=meta,
            indexed=indexed,
            vector=vector,
            **kwargs,
        )

    async def get(self, ref_or_path: RecordRef | str):
        return await self.backend.get(ref_or_path)

    async def get_data(self, ref_or_path: RecordRef | str):
        return await self.backend.get_data(ref_or_path)

    async def ref_envelope(self, ref_or_id: RecordRef | str) -> RecordReference:
        return await self.backend.ref_envelope(ref_or_id)

    async def read_bounded(
        self,
        ref_or_path: RecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> RecordContentSegment:
        return await self.backend.read_bounded(ref_or_path, offset=offset, limit=limit)

    def stream_read(
        self,
        ref_or_path: RecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[RecordContentSegment]:
        return self.backend.stream_read(
            ref_or_path,
            offset=offset,
            limit=limit,
            chunk_size=chunk_size,
        )

    async def grep(self, query: str | None = None, filters: dict[str, Any] | None = None):
        return await self.backend.search(query, self._scoped_filters(filters))

    async def search(self, query: str | None = None, filters: dict[str, Any] | None = None):
        return await self.backend.search(query, self._scoped_filters(filters))

    async def retrieve(
        self,
        query: str | None = None,
        *,
        tags: "Sequence[str] | None" = None,
        filters: dict[str, Any] | None = None,
        scope: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        selection: RecordRetrievalSelection = "length",
        top_n: int | None = None,
        method: RecordRetrievalMethod = "auto",
        rerank: bool | None = None,
        rerank_handler: RerankHandler | None = None,
        max_rerank_retries: int = 1,
        max_candidates: int | None = None,
        profile: str = "auto",
        plugin_manager: Any = None,
        settings: Any = None,
    ) -> RecordRetrievalPackage:
        return await retrieve_records(
            self,
            query,
            tags=tags,
            filters=filters,
            scope=scope,
            budget=budget,
            selection=selection,
            top_n=top_n,
            method=method,
            rerank=rerank,
            rerank_handler=rerank_handler,
            max_rerank_retries=max_rerank_retries,
            max_candidates=max_candidates,
            profile=profile,
            plugin_manager=plugin_manager,
            settings=settings,
        )

    async def link(
        self,
        source: RecordRef | str,
        target: RecordRef | str,
        relation: str,
        meta: dict[str, Any] | None = None,
    ):
        return await self.backend.link(source, target, relation, meta)

    async def links(
        self,
        ref_or_id: RecordRef | str | None = None,
        *,
        source: RecordRef | str | None = None,
        target: RecordRef | str | None = None,
        relation: str | None = None,
    ) -> list[RecordLink]:
        return await self.backend.links(ref_or_id, source=source, target=target, relation=relation)

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
    ) -> RecordLink:
        return await self.backend.link_evidence(
            source,
            target,
            relation,
            execution_id=execution_id,
            operation_id=operation_id,
            runtime_event_id=runtime_event_id,
            checkpoint_id=checkpoint_id,
            exchange_id=exchange_id,
            artifact_refs=artifact_refs,
            meta=meta,
        )

    @_guard_record_store_mutation
    async def checkpoint(self, run_id: str, state: dict[str, Any], *, step_id: str | None = None):
        ref = await self.backend.checkpoint(run_id, state, step_id=step_id)
        return await self._scope_record_ref(ref)

    @_guard_record_store_mutation
    async def put_checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> RecordRef:
        ref = await self.backend.put_checkpoint(
            run_id,
            state,
            step_id=step_id,
            expected_state_version=expected_state_version,
        )
        return await self._scope_record_ref(ref)

    async def get_checkpoint(self, run_id: str) -> RecordRef | None:
        return await self.latest_checkpoint(run_id)

    @_guard_record_store_mutation
    async def put_snapshot(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
        retention: SnapshotRetentionPolicy | None = None,
    ) -> RecordRef:
        put_snapshot = self.backend.put_snapshot
        resolved_retention = self._snapshot_retention if retention is None else retention
        put_kwargs: dict[str, Any] = {
            "step_id": step_id,
            "expected_state_version": expected_state_version,
        }
        if resolved_retention is not None:
            try:
                signature = inspect.signature(put_snapshot)
            except (TypeError, ValueError):
                signature = None
            if signature is not None:
                accepts_retention = "retention" in signature.parameters or any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
                )
                if not accepts_retention:
                    raise TypeError(
                        "RecordStore snapshot provider must accept retention=... "
                        "when snapshot retention is configured."
                    )
            put_kwargs["retention"] = resolved_retention
        ref = await put_snapshot(
            run_id,
            state,
            **put_kwargs,
        )
        return await self._scope_record_ref(ref)

    async def get_snapshot(self, run_id: str) -> dict[str, Any] | None:
        ref = await self.latest_snapshot(run_id)
        if ref is None:
            return None
        state = await self.get_data(ref)
        return state if isinstance(state, dict) else None

    async def latest_snapshot(self, run_id: str) -> RecordRef | None:
        return await self.latest_checkpoint(run_id)

    @_guard_record_store_mutation
    async def delete_snapshot(self, run_id: str) -> dict[str, Any]:
        delete_snapshot = getattr(self.backend, "delete_snapshot", None)
        if not callable(delete_snapshot):
            raise TypeError("RecordStore snapshot provider must expose async delete_snapshot(run_id).")
        delete_snapshot_async = cast(
            Callable[[str], Awaitable[dict[str, Any]]],
            delete_snapshot,
        )
        return await delete_snapshot_async(run_id)

    @_guard_record_store_mutation
    async def prune_snapshots(
        self,
        run_id: str,
        *,
        keep_last: int,
    ) -> SnapshotPruneResult:
        prune_snapshots = getattr(self.backend, "prune_snapshots", None)
        if not callable(prune_snapshots):
            raise TypeError(
                "RecordStore snapshot provider must expose "
                "async prune_snapshots(run_id, keep_last=...)."
            )
        return await cast(
            Callable[..., Awaitable[SnapshotPruneResult]],
            prune_snapshots,
        )(
            run_id,
            keep_last=keep_last,
        )

    async def latest_checkpoint(self, run_id: str) -> RecordRef | None:
        if not self.default_search_scope:
            return await self.backend.latest_checkpoint(run_id)
        history = await self.checkpoint_history(run_id)
        return history[0] if history else None

    async def checkpoint_history(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
        limit: int | None = None,
    ) -> list[RecordRef]:
        backend_limit = None if self.default_search_scope else limit
        history = await self.backend.checkpoint_history(run_id, step_id=step_id, limit=backend_limit)
        if self.default_search_scope:
            history = [ref for ref in history if self._matches_default_search_scope(ref)]
            if limit is not None:
                if limit < 0:
                    raise ValueError("limit must be greater than or equal to 0.")
                history = history[:limit]
        return history

    async def claim_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl: float,
        expected_state_version: int | None = None,
    ) -> ExecutionLease:
        return await self.backend.claim_lease(
            run_id,
            owner_id,
            ttl=ttl,
            expected_state_version=expected_state_version,
        )

    async def heartbeat_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> ExecutionLease:
        return await self.backend.heartbeat_lease(run_id, owner_id, lease_token)

    async def release_lease(
        self,
        run_id: str,
        owner_id: str,
        lease_token: str,
    ) -> ExecutionLease:
        return await self.backend.release_lease(run_id, owner_id, lease_token)

    @_guard_record_store_mutation
    async def put_artifact_ref(
        self,
        run_id: str,
        artifact: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> RecordRef:
        scoped_metadata = dict(metadata or {})
        if self.default_scope:
            raw_scope = scoped_metadata.get("scope")
            scoped_metadata["scope"] = self._scoped_record_scope(raw_scope if isinstance(raw_scope, dict) else {})
        ref = await self.backend.put_artifact_ref(run_id, artifact, metadata=scoped_metadata)
        return await self._scope_record_ref(ref)

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
    ) -> StoredRuntimeEvent:
        return await self.backend.append_runtime_event(
            execution_id,
            event,
            sequence=sequence,
            expected_sequence=expected_sequence,
            idempotency_key=idempotency_key,
            snapshot_ref=snapshot_ref,
            artifact_refs=artifact_refs,
            exchange_id=exchange_id,
            state_version=state_version,
            parent_id=parent_id,
            causation_id=causation_id,
            parent_signal_id=parent_signal_id,
            node_id=node_id,
            operator_id=operator_id,
            interrupt_id=interrupt_id,
            resume_request_id=resume_request_id,
            actor_id=actor_id,
            lease_owner_id=lease_owner_id,
            aggregation_scope=aggregation_scope,
        )

    async def query_runtime_events(
        self,
        execution_id: str,
        *,
        sequence_from: int | None = None,
        sequence_to: int | None = None,
        event_id: str | None = None,
        limit: int | None = None,
    ) -> list[StoredRuntimeEvent]:
        return await self.backend.query_runtime_events(
            execution_id,
            sequence_from=sequence_from,
            sequence_to=sequence_to,
            event_id=event_id,
            limit=limit,
        )

    def capabilities(self) -> RecordStoreCapabilities:
        materialized_components: list[str] = []
        private_write = True
        if self._backend is not None:
            backend_capabilities = dict(self._backend.capabilities())
            private_write = bool(backend_capabilities.get("private_write", False))
            components = backend_capabilities.get("materialized_components", [])
            if isinstance(components, list):
                materialized_components = sorted(str(name) for name in components)
        return cast(
            RecordStoreCapabilities,
            {
                "root": str(self.root),
                "mode": self.mode,
                "external_read": True,
                "external_write": self.mode == "read_write",
                "private_write": private_write,
                "materialized_components": materialized_components,
            },
        )

    async def ingest(
        self,
        *,
        content: Any,
        collection: str,
        kind: str | None = None,
        scope: dict[str, Any] | None = None,
        source: dict[str, Any] | None = None,
        summary: str | None = None,
        meta: dict[str, Any] | None = None,
        profile: str = "fast",
    ):
        return await self.put(
            content=content,
            collection=collection,
            kind=kind,
            scope=scope,
            source=source or {},
            summary=summary,
            meta=meta,
            profile=profile or "fast",
        )
