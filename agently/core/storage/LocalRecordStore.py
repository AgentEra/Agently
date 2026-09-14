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
import base64
import hashlib
import json
import math
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import closing, contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from agently.types.data.event import RuntimeEvent, RuntimeEventDict
from agently.types.data.record_store import (
    RecordStoreCapabilities,
    RecordContentSegment,
    ExecutionLease,
    RecordLink,
    RecordRef,
    RecordReference,
    SnapshotPruneResult,
    SnapshotRetentionPolicy,
    StoredRuntimeEvent,
)

from .Errors import RecordStorePolicyError
from .Identity import RecordIdentityCatalog
from .SnapshotRetention import normalize_snapshot_retention
from .Stores import VectorIndexPipeline


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _sanitize(value: Any) -> Any:
    return _json_loads(_json(value), None)


def _context_filter_value(value: Any) -> Any:
    """Stable values only; unsupported equality objects keep the generic source path."""
    if value is None or type(value) in {str, bool, int}:
        return [type(value).__name__, value]
    if type(value) is float and math.isfinite(value):
        return ["float", value]
    if type(value) is dict and all(type(key) is str for key in value):
        return ["dict", [[key, _context_filter_value(item)] for key, item in sorted(value.items())]]
    if type(value) in {list, tuple}:
        return [type(value).__name__, [_context_filter_value(item) for item in cast(Any, value)]]
    raise TypeError("Context filter cannot be normalized without changing its equality contract.")


class LocalRecordStore:
    """Lazy local persistence for Agently-private RecordStore state.

    ``root`` is the private ``<record_store>/.agently/records`` directory. Construction is
    path arithmetic only: neither the directory nor SQLite is created until a
    persistence operation explicitly needs it. Each feature creates only its
    own tables.
    """

    name = "local"

    def __init__(
        self,
        root: str | Path,
        *,
        create: bool = True,
        mode: str = "read_write",
        initialize_default_vector_store_provider: bool = False,
        snapshot_retention: SnapshotRetentionPolicy | None = None,
        **_: Any,
    ) -> None:
        if mode not in {"read", "read_only", "readonly", "read_write", "write"}:
            raise ValueError("RecordStore backend mode must be read_only or read_write.")
        self.root = Path(root).expanduser().resolve()
        self.db_path = self.root / "records.db"
        self.create = bool(create)
        self.read_only = mode in {"read", "read_only", "readonly"}
        self._lock = asyncio.Lock()
        self._identity_catalog = RecordIdentityCatalog(
            self.root,
            record_store_id=self.record_store_id,
            create=self.create,
            private_write=not self.read_only,
        )

        self.db_store_provider: Any = self
        self.db_store_provider_name = "sqlite"
        self.embedding_provider: Any | None = None
        self.rerank_provider: Any | None = None
        self.vector_store_provider: Any | None = None
        self.vector_store_provider_name: str | None = None
        self.vector_store_fallback_reason: str | None = None
        self.vector_index = VectorIndexPipeline(
            embedding_provider=None,
            vector_store_provider=None,
        )
        self._snapshot_retention = normalize_snapshot_retention(
            snapshot_retention,
            default_keep_last=3,
        )
        self._db_store_provider_loader: Callable[[], tuple[Any | None, str]] | None = None
        self._embedding_provider_loader: Callable[[], Any | None] | None = None
        self._rerank_provider_loader: Callable[[], Any | None] | None = None
        self._vector_store_provider_loader: Callable[[], tuple[Any | None, str | None, str | None]] | None = None
        self._db_store_provider_loaded = False
        self._embedding_provider_loaded = False
        self._rerank_provider_loaded = False
        self._vector_store_provider_loaded = False
        self._materialized_components: set[str] = set()

    @property
    def record_store_id(self) -> str:
        ordinary_root = (
            self.root.parent.parent
            if self.root.name == "records" and self.root.parent.name == ".agently"
            else self.root.parent
            if self.root.name == ".agently"
            else self.root
        )
        return hashlib.sha256(str(ordinary_root).encode("utf-8")).hexdigest()

    def configure_component_loaders(
        self,
        *,
        db_store_provider_loader: Callable[[], tuple[Any | None, str]] | None = None,
        embedding_provider_loader: Callable[[], Any | None] | None = None,
        rerank_provider_loader: Callable[[], Any | None] | None = None,
        vector_store_provider_loader: Callable[[], tuple[Any | None, str | None, str | None]] | None = None,
        db_store_provider_name: str = "sqlite",
        vector_store_provider_name: str | None = None,
    ) -> None:
        self._db_store_provider_loader = db_store_provider_loader
        self._embedding_provider_loader = embedding_provider_loader
        self._rerank_provider_loader = rerank_provider_loader
        self._vector_store_provider_loader = vector_store_provider_loader
        self.db_store_provider_name = db_store_provider_name
        self.vector_store_provider_name = vector_store_provider_name

    def _ensure_db_store_provider(self) -> Any:
        if not self._db_store_provider_loaded:
            self._db_store_provider_loaded = True
            if self._db_store_provider_loader is not None:
                provider, name = self._db_store_provider_loader()
                self.db_store_provider = provider or self
                self.db_store_provider_name = name
        return self.db_store_provider

    def ensure_rerank_provider(self) -> Any | None:
        """Materialize the optional reranker. Absence is ordinary, not an error."""
        if not self._rerank_provider_loaded:
            self._rerank_provider_loaded = True
            if self._rerank_provider_loader is not None:
                self.rerank_provider = self._rerank_provider_loader()
                if self.rerank_provider is not None:
                    self._materialized_components.add("rerank")
        return self.rerank_provider

    def ensure_vector_index(self) -> tuple[Any, Any]:
        if not self._embedding_provider_loaded:
            self._embedding_provider_loaded = True
            if self._embedding_provider_loader is not None:
                self.embedding_provider = self._embedding_provider_loader()
        if not self._vector_store_provider_loaded:
            self._vector_store_provider_loaded = True
            if self._vector_store_provider_loader is not None:
                provider, name, reason = self._vector_store_provider_loader()
                self.vector_store_provider = provider
                self.vector_store_provider_name = name
                self.vector_store_fallback_reason = reason
        if self.embedding_provider is None or self.vector_store_provider is None:
            raise RuntimeError("RecordStore vector indexing requires both embedding_provider and vector_store_provider.")
        self._materialized_components.update({"embedding", "vector"})
        self.vector_index = VectorIndexPipeline(
            embedding_provider=self.embedding_provider,
            vector_store_provider=self.vector_store_provider,
        )
        return self.embedding_provider, self.vector_store_provider

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write:
            if self.read_only:
                raise RecordStorePolicyError("RecordStore persistence backend is read-only.")
            if not self.db_path.exists():
                if not self.create:
                    raise RecordStorePolicyError("RecordStore persistence is not initialized and create=False.")
                self.root.mkdir(parents=True, exist_ok=True)
        if not self.db_path.exists() and not write:
            raise FileNotFoundError(f"RecordStore database does not exist: {self.db_path}")
        connection = sqlite3.connect(self.db_path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _create_records_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY,
                collection TEXT NOT NULL,
                kind TEXT,
                content TEXT NOT NULL,
                content_format TEXT NOT NULL,
                path TEXT,
                sha256 TEXT,
                size INTEGER NOT NULL,
                summary TEXT NOT NULL,
                scope TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                meta TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _serialize_content(content: Any) -> tuple[str, str, bytes]:
        if isinstance(content, str):
            raw = content.encode("utf-8")
            return content, "text", raw
        if isinstance(content, (bytes, bytearray)):
            raw = bytes(content)
            return base64.b64encode(raw).decode("ascii"), "bytes", raw
        text = _json(content)
        return text, "json", text.encode("utf-8")

    @staticmethod
    def _decode_content(content: str, content_format: str) -> Any:
        if content_format == "text":
            return content
        if content_format == "bytes":
            return base64.b64decode(content.encode("ascii"))
        return _json_loads(content, content)

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _row_to_ref(row: sqlite3.Row) -> RecordRef:
        return {
            "id": str(row["id"]),
            "collection": str(row["collection"]),
            "kind": str(row["kind"]) if row["kind"] is not None else None,
            "path": str(row["path"]) if row["path"] is not None else None,
            "sha256": str(row["sha256"]) if row["sha256"] is not None else None,
            "size": int(row["size"]),
            "summary": str(row["summary"]),
            "scope": cast(dict[str, Any], _json_loads(row["scope"], {})),
            "source": cast(dict[str, Any], _json_loads(row["source"], {})),
            "created_at": str(row["created_at"]),
            "meta": cast(dict[str, Any], _json_loads(row["meta"], {})),
        }

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
        **_: Any,
    ) -> RecordRef:
        if not str(collection).strip():
            raise ValueError("RecordStore collection cannot be empty.")
        stored, content_format, raw = self._serialize_content(content)
        created_at = _now()
        record_id = (await self._identity_catalog.allocate("record")).entity_id
        ref: RecordRef = {
            "id": record_id,
            "collection": str(collection),
            "kind": kind,
            "path": None,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "summary": str(summary or ""),
            "scope": dict(scope or {}),
            "source": dict(source or {}),
            "created_at": created_at,
            "meta": dict(meta or {}),
        }
        async with self._lock:
            with self._connect(write=True) as connection:
                self._create_records_table(connection)
                connection.execute(
                    """
                    INSERT INTO records (
                        id, collection, kind, content, content_format, path, sha256,
                        size, summary, scope, source, created_at, meta
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        ref["collection"],
                        kind,
                        stored,
                        content_format,
                        None,
                        ref["sha256"],
                        ref["size"],
                        ref["summary"],
                        _json(ref["scope"]),
                        _json(ref["source"]),
                        created_at,
                        _json(ref["meta"]),
                    ),
                )
                if indexed:
                    self._create_fts_table(connection)
                    connection.execute(
                        "INSERT INTO records_fts (id, content, summary) VALUES (?, ?, ?)",
                        (record_id, self._content_text(content), ref["summary"]),
                    )
                connection.commit()
        self._materialized_components.add("records")

        provider = self._ensure_db_store_provider()
        if provider is not self:
            await provider.put_record(ref)
            if indexed:
                await provider.index_record(ref, self._content_text(content))
        if vector:
            embedding_provider, vector_provider = self.ensure_vector_index()
            embeddings = await embedding_provider.embed_texts([self._content_text(content)])
            if not embeddings:
                raise RuntimeError("RecordStore embedding provider returned no embedding.")
            await vector_provider.index_record(ref, embeddings[0])
        return ref

    async def put_record(self, ref: RecordRef) -> RecordRef:
        async with self._lock:
            with self._connect(write=True) as connection:
                self._create_records_table(connection)
                existing = connection.execute("SELECT id FROM records WHERE id = ?", (ref["id"],)).fetchone()
                if existing is None:
                    raise KeyError("RecordStore.put_record(...) can update metadata only for a locally stored record.")
                connection.execute(
                    """
                    UPDATE records SET collection = ?, kind = ?, path = ?, sha256 = ?,
                        size = ?, summary = ?, scope = ?, source = ?, created_at = ?, meta = ?
                    WHERE id = ?
                    """,
                    (
                        ref["collection"],
                        ref.get("kind"),
                        ref.get("path"),
                        ref.get("sha256"),
                        int(ref.get("size", 0)),
                        str(ref.get("summary", "")),
                        _json(ref.get("scope", {})),
                        _json(ref.get("source", {})),
                        str(ref.get("created_at") or _now()),
                        _json(ref.get("meta", {})),
                        ref["id"],
                    ),
                )
                connection.commit()
        provider = self._ensure_db_store_provider()
        if provider is not self:
            await provider.put_record(ref)
        return ref

    async def get_record(self, record_id: str) -> RecordRef | None:
        if not self.db_path.exists():
            return None
        with self._connect() as connection:
            if not self._table_exists(connection, "records"):
                return None
            row = connection.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        return self._row_to_ref(row) if row is not None else None

    async def index_record(self, ref: RecordRef, content: str) -> None:
        async with self._lock:
            with self._connect(write=True) as connection:
                self._create_fts_table(connection)
                connection.execute("DELETE FROM records_fts WHERE id = ?", (ref["id"],))
                connection.execute(
                    "INSERT INTO records_fts (id, content, summary) VALUES (?, ?, ?)",
                    (ref["id"], content, str(ref.get("summary", ""))),
                )
                connection.commit()
        self._materialized_components.add("text_index")

    @staticmethod
    def _create_fts_table(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(id UNINDEXED, content, summary)")

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
        return connection.execute("SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (name,)).fetchone() is not None

    @staticmethod
    def _record_id(ref_or_id: RecordRef | str) -> str:
        return str(ref_or_id.get("id")) if isinstance(ref_or_id, dict) else str(ref_or_id)

    async def get(self, ref_or_path: RecordRef | str) -> str:
        data = await self.get_data(ref_or_path)
        return self._content_text(data)

    async def get_data(self, ref_or_path: RecordRef | str) -> Any:
        record_id = self._record_id(ref_or_path)
        if not self.db_path.exists():
            raise KeyError(f"RecordStore record not found: {record_id}")
        with self._connect() as connection:
            if not self._table_exists(connection, "records"):
                raise KeyError(f"RecordStore record not found: {record_id}")
            row = connection.execute(
                "SELECT content, content_format FROM records WHERE id = ?", (record_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"RecordStore record not found: {record_id}")
        return self._decode_content(str(row["content"]), str(row["content_format"]))

    async def ref_envelope(self, ref_or_id: RecordRef | str) -> RecordReference:
        ref = ref_or_id if isinstance(ref_or_id, dict) else await self.get_record(str(ref_or_id))
        if ref is None:
            raise KeyError(f"RecordStore record not found: {ref_or_id}")
        return self._ref_envelope(ref)

    def _ref_envelope(self, ref: RecordRef) -> RecordReference:
        return {
            "record_store_id": self.record_store_id,
            "kind": str(ref.get("kind") or "record"),
            "collection": str(ref["collection"]),
            "record_id": str(ref["id"]),
            "version": None,
            "content_ref": None,
            "digest": ref.get("sha256"),
            "size": int(ref.get("size", 0)),
            "created_at": str(ref.get("created_at") or ""),
            "policy_labels": list(ref.get("meta", {}).get("policy_labels", [])),
            "backend_capabilities": {"bounded_read": True, "stream_read": True},
        }

    def _context_snapshot(
        self,
        filters: dict[str, Any],
        *,
        record_store_id: str,
        page: tuple[int, int] | None = None,
        exact: tuple[str, int, int] | None = None,
        projection_limit: int = 2000,
    ) -> tuple[str, tuple[RecordRef, ...], dict[str, RecordContentSegment]] | None:
        """Read scoped metadata and requested bodies from one local SQLite view.

        Legitimate body writes insert their digest atomically. Revision scans
        metadata, never all bodies; very large metadata/row counts still cost O(n).
        None selects the unchanged generic path for non-normalizable filters.
        """
        normalized: dict[str, Any] = {}
        try:
            for key, expected in filters.items():
                if type(key) is not str:
                    return None
                if type(expected) in {list, tuple, set}:
                    normalized[key] = ["in", sorted({_json(_context_filter_value(item)) for item in expected})]
                else:
                    normalized[key] = ["eq", _context_filter_value(expected)]
        except TypeError:
            return None
        if exact is not None and (exact[1] < 0 or exact[2] < 0):
            raise ValueError("RecordStore read offset and limit must be non-negative.")
        digest = hashlib.sha256(_json({
            "algorithm": "record-store-scope/v1",
            "record_store_id": record_store_id,
            "filters": normalized,
        }).encode("utf-8"))
        try:
            self.db_path.stat()
            exists = True
        except FileNotFoundError:
            exists = False
        manager = (
            closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=30))
            if exists else nullcontext(None)
        )
        with manager as connection:
            try:
                rows: list[sqlite3.Row] = []
                if connection is not None:
                    connection.row_factory = sqlite3.Row
                    connection.execute("BEGIN")
                    if self._table_exists(connection, "records"):
                        rows = connection.execute(
                            "SELECT id, collection, kind, path, sha256, size, summary, scope, source, "
                            "created_at, meta, content_format FROM records ORDER BY created_at DESC, id DESC"
                        ).fetchall()
                refs: list[RecordRef] = []
                metadata: list[dict[str, Any]] = []
                for row in rows:
                    ref = self._row_to_ref(row)
                    if self._matches_filters(ref, filters):
                        refs.append(ref)
                        metadata.append({**ref, "content_format": str(row["content_format"])})
                for item in sorted(metadata, key=lambda item: str(item["id"])):
                    payload = _json(item).encode("utf-8")
                    digest.update(len(payload).to_bytes(8, "big"))
                    digest.update(payload)
                selected = refs[page[0]:page[0] + page[1]] if page is not None else []
                offset, limit = 0, projection_limit
                if exact is not None:
                    selected = [ref for ref in refs if ref["id"] == exact[0]]
                    if not selected:
                        raise KeyError(f"RecordStore context record is not visible: {exact[0]}")
                    offset, limit = exact[1], exact[2]
                selected_refs = {ref["id"]: ref for ref in selected}
                bodies: dict[str, RecordContentSegment] = {}
                record_ids = list(selected_refs)
                for start in range(0, len(record_ids), 256):
                    assert connection is not None
                    batch = record_ids[start:start + 256]
                    placeholders = ",".join("?" for _ in batch)
                    body_rows = connection.execute(
                        f"SELECT id, content, content_format FROM records WHERE id IN ({placeholders})", batch
                    ).fetchall()
                    for row in body_rows:
                        record_id = str(row["id"])
                        data = self._decode_content(str(row["content"]), str(row["content_format"]))
                        raw = self._content_text(data).encode("utf-8")
                        end = min(len(raw), offset + limit)
                        segment = raw[offset:end]
                        bodies[record_id] = {
                            "ref": self._ref_envelope(selected_refs[record_id]),
                            "content": segment.decode("utf-8", errors="replace"),
                            "offset": offset,
                            "size": len(segment),
                            "total_size": len(raw),
                            "eof": end >= len(raw),
                            "digest": hashlib.sha256(raw).hexdigest(),
                            "content_type": "text/plain",
                        }
                return f"record-store-revision:{digest.hexdigest()}", tuple(refs), bodies
            finally:
                if connection is not None:
                    connection.rollback()

    async def read_bounded(
        self,
        ref_or_path: RecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> RecordContentSegment:
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("RecordStore read offset and limit must be non-negative.")
        data = await self.get_data(ref_or_path)
        raw = self._content_text(data).encode("utf-8")
        end = len(raw) if limit is None else min(len(raw), offset + limit)
        segment = raw[offset:end]
        return {
            "ref": await self.ref_envelope(ref_or_path),
            "content": segment.decode("utf-8", errors="replace"),
            "offset": offset,
            "size": len(segment),
            "total_size": len(raw),
            "eof": end >= len(raw),
            "digest": hashlib.sha256(raw).hexdigest(),
            "content_type": "text/plain",
        }

    def stream_read(
        self,
        ref_or_path: RecordRef | str,
        *,
        offset: int = 0,
        limit: int | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[RecordContentSegment]:
        if chunk_size <= 0:
            raise ValueError("RecordStore stream chunk_size must be positive.")

        async def generate() -> AsyncIterator[RecordContentSegment]:
            consumed = 0
            while limit is None or consumed < limit:
                current_limit = chunk_size if limit is None else min(chunk_size, limit - consumed)
                segment = await self.read_bounded(ref_or_path, offset=offset + consumed, limit=current_limit)
                yield segment
                consumed += segment["size"]
                if segment["eof"] or segment["size"] == 0:
                    break

        return generate()

    async def search(
        self,
        query: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[RecordRef]:
        if not self.db_path.exists():
            return []
        with self._connect() as connection:
            if not self._table_exists(connection, "records"):
                return []
            rows = connection.execute("SELECT * FROM records ORDER BY created_at DESC, id DESC").fetchall()
        needle = str(query or "").casefold()
        resolved_filters = dict(filters or {})
        results: list[RecordRef] = []
        for row in rows:
            ref = self._row_to_ref(row)
            if needle and needle not in f"{row['content']}\n{row['summary']}".casefold():
                continue
            if not self._matches_filters(ref, resolved_filters):
                continue
            results.append(ref)
        return results

    @staticmethod
    def _matches_filters(ref: RecordRef, filters: dict[str, Any]) -> bool:
        for key, expected in filters.items():
            if "." in key:
                prefix, child = key.split(".", 1)
                container = ref.get(prefix) if prefix in {"scope", "source", "meta"} else None
                actual = container.get(child) if isinstance(container, dict) else None
            else:
                actual = ref.get(key)  # type: ignore[literal-required]
            if isinstance(expected, (list, tuple, set)):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    @staticmethod
    def _create_links_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS links (
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, target_id TEXT NOT NULL,
                relation TEXT NOT NULL, created_at TEXT NOT NULL, meta TEXT NOT NULL
            )
            """
        )

    async def link(
        self,
        source: RecordRef | str,
        target: RecordRef | str,
        relation: str,
        meta: dict[str, Any] | None = None,
    ) -> RecordLink:
        record: RecordLink = {
            "id": (await self._identity_catalog.allocate("link")).entity_id,
            "source_id": self._record_id(source),
            "target_id": self._record_id(target),
            "relation": str(relation),
            "created_at": _now(),
            "meta": dict(meta or {}),
        }
        async with self._lock:
            with self._connect(write=True) as connection:
                self._create_links_table(connection)
                connection.execute(
                    "INSERT INTO links VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        record["id"],
                        record["source_id"],
                        record["target_id"],
                        record["relation"],
                        record["created_at"],
                        _json(record["meta"]),
                    ),
                )
                connection.commit()
        self._materialized_components.add("links")
        return record

    async def links(
        self,
        ref_or_id: RecordRef | str | None = None,
        *,
        source: RecordRef | str | None = None,
        target: RecordRef | str | None = None,
        relation: str | None = None,
    ) -> list[RecordLink]:
        if not self.db_path.exists():
            return []
        with self._connect() as connection:
            if not self._table_exists(connection, "links"):
                return []
            rows = connection.execute("SELECT * FROM links ORDER BY created_at, id").fetchall()
        any_id = self._record_id(ref_or_id) if ref_or_id is not None else None
        source_id = self._record_id(source) if source is not None else None
        target_id = self._record_id(target) if target is not None else None
        output: list[RecordLink] = []
        for row in rows:
            if any_id and row["source_id"] != any_id and row["target_id"] != any_id:
                continue
            if source_id and row["source_id"] != source_id:
                continue
            if target_id and row["target_id"] != target_id:
                continue
            if relation and row["relation"] != relation:
                continue
            output.append(
                {
                    "id": str(row["id"]),
                    "source_id": str(row["source_id"]),
                    "target_id": str(row["target_id"]),
                    "relation": str(row["relation"]),
                    "created_at": str(row["created_at"]),
                    "meta": cast(dict[str, Any], _json_loads(row["meta"], {})),
                }
            )
        return output

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
        evidence_meta = dict(meta or {})
        for key, value in {
            "execution_id": execution_id,
            "operation_id": operation_id,
            "runtime_event_id": runtime_event_id,
            "checkpoint_id": checkpoint_id,
            "exchange_id": exchange_id,
        }.items():
            if value is not None:
                evidence_meta[key] = value
        if artifact_refs:
            evidence_meta["artifact_refs"] = [
                item.get("record_id") or item.get("id") if isinstance(item, dict) else str(item)
                for item in artifact_refs
            ]
        return await self.link(source, target, relation, evidence_meta)

    @staticmethod
    def _create_recovery_tables(connection: sqlite3.Connection) -> None:
        LocalRecordStore._create_records_table(connection)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                step_id TEXT, record_id TEXT NOT NULL, state_version INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS manifests (
                run_id TEXT PRIMARY KEY, latest_record_id TEXT NOT NULL,
                state_version INTEGER, updated_at TEXT NOT NULL
            )
            """
        )

    async def checkpoint(self, run_id: str, state: dict[str, Any], *, step_id: str | None = None) -> RecordRef:
        return await self.put_checkpoint(run_id, state, step_id=step_id)

    async def put_checkpoint(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
    ) -> RecordRef:
        return await self._put_recovery_record(
            run_id,
            state,
            step_id=step_id,
            expected_state_version=expected_state_version,
            snapshot_keep_last=None,
        )

    async def _put_recovery_record(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None,
        expected_state_version: int | None,
        snapshot_keep_last: int | None,
    ) -> RecordRef:
        state_version_value = state.get("state_version")
        state_version = int(state_version_value) if state_version_value is not None else None
        deleted_record_ids: set[str] = set()
        deleted_link_ids: set[str] = set()
        async with self._lock:
            record_id = (await self._identity_catalog.allocate("record")).entity_id
            with self._connect(write=True) as connection:
                self._create_recovery_tables(connection)
                latest = connection.execute(
                    "SELECT state_version FROM manifests WHERE run_id = ?", (run_id,)
                ).fetchone()
                current_version = int(latest["state_version"] or 0) if latest else 0
                if expected_state_version is not None and current_version != expected_state_version:
                    raise RuntimeError(
                        f"RecordStore state version conflict for run '{run_id}': "
                        f"expected {expected_state_version}, current is {current_version}."
                    )
                stored, content_format, raw = self._serialize_content(state)
                created_at = _now()
                scope = {"run_id": run_id}
                if step_id is not None:
                    scope["step_id"] = step_id
                ref: RecordRef = {
                    "id": record_id,
                    "collection": "checkpoints",
                    "kind": "snapshot",
                    "path": None,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size": len(raw),
                    "summary": f"Checkpoint for {run_id}",
                    "scope": scope,
                    "source": {"type": "record_store_recovery"},
                    "created_at": created_at,
                    "meta": {"state_version": state_version},
                }
                connection.execute(
                    """
                    INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        "checkpoints",
                        "snapshot",
                        stored,
                        content_format,
                        None,
                        ref["sha256"],
                        ref["size"],
                        ref["summary"],
                        _json(scope),
                        _json(ref["source"]),
                        created_at,
                        _json(ref["meta"]),
                    ),
                )
                connection.execute(
                    "INSERT INTO checkpoints (run_id, step_id, record_id, state_version, created_at) VALUES (?, ?, ?, ?, ?)",
                    (run_id, step_id, record_id, state_version, created_at),
                )
                connection.execute(
                    """
                    INSERT INTO manifests (run_id, latest_record_id, state_version, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET latest_record_id = excluded.latest_record_id,
                        state_version = excluded.state_version, updated_at = excluded.updated_at
                    """,
                    (run_id, record_id, state_version, created_at),
                )
                if snapshot_keep_last is not None:
                    deleted_record_ids, deleted_link_ids, _ = self._prune_snapshot_records(
                        connection,
                        run_id=run_id,
                        keep_last=snapshot_keep_last,
                    )
                connection.commit()
        if deleted_record_ids or deleted_link_ids:
            await self._identity_catalog.discard([*sorted(deleted_record_ids), *sorted(deleted_link_ids)])
        self._materialized_components.update({"records", "recovery"})
        return ref

    async def put_snapshot(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        step_id: str | None = None,
        expected_state_version: int | None = None,
        retention: SnapshotRetentionPolicy | None = None,
    ) -> RecordRef:
        resolved_retention = normalize_snapshot_retention(
            retention,
            default_keep_last=self._snapshot_retention["keep_last"],
        )
        return await self._put_recovery_record(
            run_id,
            state,
            step_id=step_id,
            expected_state_version=expected_state_version,
            snapshot_keep_last=resolved_retention["keep_last"],
        )

    def _prune_snapshot_records(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        keep_last: int,
    ) -> tuple[set[str], set[str], int]:
        rows = connection.execute(
            """
            SELECT record_id FROM checkpoints
            WHERE run_id = ?
            ORDER BY id DESC
            LIMIT -1 OFFSET ?
            """,
            (run_id, keep_last),
        ).fetchall()
        record_ids = {str(row["record_id"]) for row in rows}
        if not record_ids:
            return set(), set(), 0

        placeholders = ",".join("?" for _ in record_ids)
        parameters = tuple(sorted(record_ids))
        size_row = connection.execute(
            f"SELECT COALESCE(SUM(size), 0) AS value FROM records WHERE id IN ({placeholders})",
            parameters,
        ).fetchone()
        deleted_bytes = int(size_row["value"] or 0) if size_row is not None else 0
        link_ids: set[str] = set()
        if self._table_exists(connection, "records_fts"):
            connection.execute(
                f"DELETE FROM records_fts WHERE id IN ({placeholders})",
                parameters,
            )
        if self._table_exists(connection, "record_store_vectors"):
            connection.execute(
                f"DELETE FROM record_store_vectors WHERE record_id IN ({placeholders})",
                parameters,
            )
        if self._table_exists(connection, "links"):
            link_rows = connection.execute(
                f"SELECT id FROM links WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
                (*parameters, *parameters),
            ).fetchall()
            link_ids.update(str(row["id"]) for row in link_rows)
            connection.execute(
                f"DELETE FROM links WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
                (*parameters, *parameters),
            )
        connection.execute(
            f"DELETE FROM checkpoints WHERE record_id IN ({placeholders})",
            parameters,
        )
        connection.execute(
            f"DELETE FROM records WHERE id IN ({placeholders})",
            parameters,
        )
        return record_ids, link_ids, deleted_bytes

    async def prune_snapshots(
        self,
        run_id: str,
        *,
        keep_last: int,
    ) -> SnapshotPruneResult:
        resolved_retention = normalize_snapshot_retention(
            {"keep_last": keep_last},
            default_keep_last=None,
        )
        resolved_keep_last = resolved_retention["keep_last"]
        if resolved_keep_last is None:
            raise TypeError("snapshot prune keep_last must be a positive integer.")
        if not self.db_path.exists():
            return {
                "run_id": run_id,
                "keep_last": resolved_keep_last,
                "retained_records": 0,
                "deleted_records": 0,
                "deleted_bytes": 0,
            }

        deleted_record_ids: set[str] = set()
        deleted_link_ids: set[str] = set()
        deleted_bytes = 0
        retained_records = 0
        async with self._lock:
            with self._connect(write=True) as connection:
                if self._table_exists(connection, "checkpoints"):
                    deleted_record_ids, deleted_link_ids, deleted_bytes = self._prune_snapshot_records(
                        connection,
                        run_id=run_id,
                        keep_last=resolved_keep_last,
                    )
                    retained_row = connection.execute(
                        "SELECT COUNT(*) AS value FROM checkpoints WHERE run_id = ?",
                        (run_id,),
                    ).fetchone()
                    retained_records = int(retained_row["value"] or 0) if retained_row is not None else 0
                connection.commit()

        if deleted_record_ids or deleted_link_ids:
            await self._identity_catalog.discard([*sorted(deleted_record_ids), *sorted(deleted_link_ids)])
        return {
            "run_id": run_id,
            "keep_last": resolved_keep_last,
            "retained_records": retained_records,
            "deleted_records": len(deleted_record_ids),
            "deleted_bytes": deleted_bytes,
        }

    async def latest_checkpoint(self, run_id: str) -> RecordRef | None:
        if not self.db_path.exists():
            return None
        with self._connect() as connection:
            if not self._table_exists(connection, "manifests"):
                return None
            row = connection.execute(
                """
                SELECT records.* FROM manifests
                JOIN records ON records.id = manifests.latest_record_id
                WHERE manifests.run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return self._row_to_ref(row) if row is not None else None

    async def get_checkpoint(self, run_id: str) -> RecordRef | None:
        return await self.latest_checkpoint(run_id)

    async def latest_snapshot(self, run_id: str) -> RecordRef | None:
        return await self.latest_checkpoint(run_id)

    async def delete_snapshot(self, run_id: str) -> dict[str, Any]:
        """Delete one run's transient recovery closure.

        Runtime events are intentionally excluded: they belong to the explicit
        audit port, not to snapshot recovery.  When recovery was the database's
        only materialized capability, the empty SQLite carrier is reclaimed as
        well.
        """

        if not self.db_path.exists():
            return {
                "run_id": run_id,
                "deleted_records": 0,
                "deleted_bytes": 0,
                "database_removed": False,
            }

        deleted_record_ids: set[str] = set()
        deleted_link_ids: set[str] = set()
        deleted_bytes = 0
        remove_database = False
        async with self._lock:
            with self._connect(write=True) as connection:
                if self._table_exists(connection, "checkpoints"):
                    rows = connection.execute(
                        "SELECT record_id FROM checkpoints WHERE run_id = ?",
                        (run_id,),
                    ).fetchall()
                    deleted_record_ids.update(str(row["record_id"]) for row in rows)

                if self._table_exists(connection, "records"):
                    rows = connection.execute("SELECT id, size, scope, source, meta FROM records").fetchall()
                    for row in rows:
                        scope = _json_loads(row["scope"], {})
                        source = _json_loads(row["source"], {})
                        meta = _json_loads(row["meta"], {})
                        if not isinstance(scope, dict) or str(scope.get("run_id") or "") != run_id:
                            continue
                        source_type = str(source.get("type") or "") if isinstance(source, dict) else ""
                        generated_by = str(meta.get("generated_by") or "") if isinstance(meta, dict) else ""
                        if source_type == "record_store_recovery" or generated_by == "triggerflow.compaction_policy":
                            deleted_record_ids.add(str(row["id"]))

                    if deleted_record_ids:
                        placeholders = ",".join("?" for _ in deleted_record_ids)
                        parameters = tuple(sorted(deleted_record_ids))
                        size_row = connection.execute(
                            f"SELECT COALESCE(SUM(size), 0) AS value FROM records WHERE id IN ({placeholders})",
                            parameters,
                        ).fetchone()
                        deleted_bytes = int(size_row["value"] or 0) if size_row is not None else 0
                        if self._table_exists(connection, "records_fts"):
                            connection.execute(
                                f"DELETE FROM records_fts WHERE id IN ({placeholders})",
                                parameters,
                            )
                        if self._table_exists(connection, "record_store_vectors"):
                            connection.execute(
                                f"DELETE FROM record_store_vectors WHERE record_id IN ({placeholders})",
                                parameters,
                            )
                        if self._table_exists(connection, "links"):
                            link_rows = connection.execute(
                                f"SELECT id FROM links WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
                                (*parameters, *parameters),
                            ).fetchall()
                            deleted_link_ids.update(str(row["id"]) for row in link_rows)
                            connection.execute(
                                f"DELETE FROM links WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
                                (*parameters, *parameters),
                            )
                        connection.execute(
                            f"DELETE FROM records WHERE id IN ({placeholders})",
                            parameters,
                        )

                for table in ("checkpoints", "manifests", "leases"):
                    if self._table_exists(connection, table):
                        connection.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))

                connection.commit()
                semantic_tables = (
                    "records",
                    "records_fts",
                    "record_store_vectors",
                    "links",
                    "checkpoints",
                    "manifests",
                    "leases",
                    "runtime_events",
                )
                remove_database = not any(
                    self._table_exists(connection, table)
                    and connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
                    for table in semantic_tables
                )

            if remove_database:
                for candidate in (
                    self.db_path,
                    Path(f"{self.db_path}-wal"),
                    Path(f"{self.db_path}-shm"),
                    Path(f"{self.db_path}-journal"),
                ):
                    try:
                        candidate.unlink()
                    except FileNotFoundError:
                        pass
                try:
                    self.root.rmdir()
                except OSError:
                    pass
                self._materialized_components.clear()
            else:
                if self._table_has_rows_from_disk("records") is False:
                    self._materialized_components.discard("records")
                self._materialized_components.discard("recovery")

        await self._identity_catalog.discard([*sorted(deleted_record_ids), *sorted(deleted_link_ids)])

        return {
            "run_id": run_id,
            "deleted_records": len(deleted_record_ids),
            "deleted_bytes": deleted_bytes,
            "database_removed": remove_database,
        }

    def _table_has_rows_from_disk(self, name: str) -> bool:
        if not self.db_path.exists():
            return False
        with self._connect() as connection:
            return (
                self._table_exists(connection, name)
                and connection.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone() is not None
            )

    async def get_snapshot(self, run_id: str) -> dict[str, Any] | None:
        ref = await self.latest_checkpoint(run_id)
        if ref is None:
            return None
        data = await self.get_data(ref)
        return data if isinstance(data, dict) else None

    async def checkpoint_history(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
        limit: int | None = None,
    ) -> list[RecordRef]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative.")
        if not self.db_path.exists():
            return []
        with self._connect() as connection:
            if not self._table_exists(connection, "checkpoints"):
                return []
            query = (
                "SELECT records.* FROM checkpoints JOIN records ON records.id = checkpoints.record_id "
                "WHERE checkpoints.run_id = ?"
            )
            params: list[Any] = [run_id]
            if step_id is not None:
                query += " AND checkpoints.step_id = ?"
                params.append(step_id)
            query += " ORDER BY checkpoints.id DESC"
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_ref(row) for row in rows]

    @staticmethod
    def _create_leases_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS leases (
                run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, lease_token TEXT NOT NULL,
                lease_ttl REAL NOT NULL, lease_until REAL NOT NULL, claimed_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL, released_at TEXT, state_version INTEGER
            )
            """
        )

    @staticmethod
    def _row_to_lease(row: sqlite3.Row) -> ExecutionLease:
        return {
            "run_id": str(row["run_id"]),
            "owner_id": str(row["owner_id"]),
            "lease_token": str(row["lease_token"]),
            "lease_ttl": float(row["lease_ttl"]),
            "lease_until": float(row["lease_until"]),
            "claimed_at": str(row["claimed_at"]),
            "heartbeat_at": str(row["heartbeat_at"]),
            "released_at": str(row["released_at"]) if row["released_at"] else None,
            "state_version": int(row["state_version"]) if row["state_version"] is not None else None,
        }

    async def claim_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl: float,
        expected_state_version: int | None = None,
    ) -> ExecutionLease:
        if ttl <= 0:
            raise ValueError("RecordStore lease ttl must be positive.")
        now_epoch = time.time()
        now_text = _now()
        async with self._lock:
            with self._connect(write=True) as connection:
                self._create_leases_table(connection)
                existing = connection.execute("SELECT * FROM leases WHERE run_id = ?", (run_id,)).fetchone()
                if (
                    existing is not None
                    and existing["released_at"] is None
                    and float(existing["lease_until"]) > now_epoch
                    and str(existing["owner_id"]) != owner_id
                ):
                    raise RuntimeError(f"RecordStore lease conflict for run '{run_id}'.")
                token = uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO leases VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(run_id) DO UPDATE SET owner_id = excluded.owner_id,
                        lease_token = excluded.lease_token, lease_ttl = excluded.lease_ttl,
                        lease_until = excluded.lease_until, claimed_at = excluded.claimed_at,
                        heartbeat_at = excluded.heartbeat_at, released_at = NULL,
                        state_version = excluded.state_version
                    """,
                    (
                        run_id,
                        owner_id,
                        token,
                        float(ttl),
                        now_epoch + float(ttl),
                        now_text,
                        now_text,
                        expected_state_version,
                    ),
                )
                connection.commit()
                row = connection.execute("SELECT * FROM leases WHERE run_id = ?", (run_id,)).fetchone()
        self._materialized_components.add("recovery")
        return self._row_to_lease(cast(sqlite3.Row, row))

    async def heartbeat_lease(self, run_id: str, owner_id: str, lease_token: str) -> ExecutionLease:
        now_epoch = time.time()
        now_text = _now()
        async with self._lock:
            with self._connect(write=True) as connection:
                if not self._table_exists(connection, "leases"):
                    raise RuntimeError(f"RecordStore lease is unavailable for run '{run_id}'.")
                row = connection.execute("SELECT * FROM leases WHERE run_id = ?", (run_id,)).fetchone()
                if (
                    row is None
                    or row["released_at"] is not None
                    or str(row["owner_id"]) != owner_id
                    or str(row["lease_token"]) != lease_token
                    or float(row["lease_until"]) <= now_epoch
                ):
                    raise RuntimeError(f"RecordStore lease conflict or expired lease for run '{run_id}'.")
                connection.execute(
                    "UPDATE leases SET lease_until = ?, heartbeat_at = ? WHERE run_id = ?",
                    (now_epoch + float(row["lease_ttl"]), now_text, run_id),
                )
                connection.commit()
                row = connection.execute("SELECT * FROM leases WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_lease(cast(sqlite3.Row, row))

    async def release_lease(self, run_id: str, owner_id: str, lease_token: str) -> ExecutionLease:
        async with self._lock:
            with self._connect(write=True) as connection:
                if not self._table_exists(connection, "leases"):
                    raise RuntimeError(f"RecordStore lease is unavailable for run '{run_id}'.")
                row = connection.execute("SELECT * FROM leases WHERE run_id = ?", (run_id,)).fetchone()
                if row is None or str(row["owner_id"]) != owner_id or str(row["lease_token"]) != lease_token:
                    raise RuntimeError(f"RecordStore lease conflict for run '{run_id}'.")
                connection.execute("UPDATE leases SET released_at = ? WHERE run_id = ?", (_now(), run_id))
                connection.commit()
                row = connection.execute("SELECT * FROM leases WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_lease(cast(sqlite3.Row, row))

    async def put_artifact_ref(
        self,
        run_id: str,
        artifact: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> RecordRef:
        details = dict(metadata or {})
        scope = dict(details.pop("scope", {}) or {})
        scope["run_id"] = run_id
        return await self.put(
            artifact,
            collection="artifacts",
            kind=str(details.pop("kind", "artifact_ref")),
            summary=str(details.pop("summary", "")),
            scope=scope,
            source={"type": "record_store_artifact_ref"},
            meta=details,
        )

    @staticmethod
    def _create_runtime_events_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_events (
                id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL, event_type TEXT NOT NULL, state_version INTEGER,
                idempotency_key TEXT, parent_id TEXT, causation_id TEXT, parent_signal_id TEXT,
                node_id TEXT, operator_id TEXT, interrupt_id TEXT, resume_request_id TEXT,
                actor_id TEXT, lease_owner_id TEXT, aggregation_scope TEXT,
                snapshot_ref TEXT, exchange_id TEXT, artifact_refs TEXT NOT NULL,
                event TEXT NOT NULL, created_at TEXT NOT NULL, persisted_at TEXT,
                UNIQUE(execution_id, sequence), UNIQUE(execution_id, idempotency_key)
            )
            """
        )

    async def _optional_envelope(
        self, ref: RecordRef | RecordReference | str | None
    ) -> RecordReference | None:
        if ref is None:
            return None
        if isinstance(ref, dict) and "record_id" in ref:
            return cast(RecordReference, dict(ref))
        try:
            return await self.ref_envelope(cast(RecordRef | str, ref))
        except KeyError:
            return None

    @staticmethod
    def _row_to_runtime_event(row: sqlite3.Row) -> StoredRuntimeEvent:
        return {
            "id": str(row["id"]),
            "execution_id": str(row["execution_id"]),
            "sequence": int(row["sequence"]),
            "event_id": str(row["event_id"]),
            "event_type": str(row["event_type"]),
            "state_version": int(row["state_version"]) if row["state_version"] is not None else None,
            "idempotency_key": str(row["idempotency_key"]) if row["idempotency_key"] else None,
            "parent_id": str(row["parent_id"]) if row["parent_id"] else None,
            "causation_id": str(row["causation_id"]) if row["causation_id"] else None,
            "parent_signal_id": str(row["parent_signal_id"]) if row["parent_signal_id"] else None,
            "node_id": str(row["node_id"]) if row["node_id"] else None,
            "operator_id": str(row["operator_id"]) if row["operator_id"] else None,
            "interrupt_id": str(row["interrupt_id"]) if row["interrupt_id"] else None,
            "resume_request_id": str(row["resume_request_id"]) if row["resume_request_id"] else None,
            "actor_id": str(row["actor_id"]) if row["actor_id"] else None,
            "lease_owner_id": str(row["lease_owner_id"]) if row["lease_owner_id"] else None,
            "aggregation_scope": str(row["aggregation_scope"]) if row["aggregation_scope"] else None,
            "snapshot_ref": cast(RecordReference | None, _json_loads(row["snapshot_ref"], None)),
            "exchange_id": str(row["exchange_id"]) if row["exchange_id"] else None,
            "artifact_refs": cast(list[RecordReference], _json_loads(row["artifact_refs"], [])),
            "event": cast(dict[str, Any], _json_loads(row["event"], {})),
            "created_at": str(row["created_at"]),
            "persisted_at": str(row["persisted_at"]) if row["persisted_at"] else None,
        }

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
        event_data = event.model_dump(mode="json") if isinstance(event, RuntimeEvent) else dict(event)
        event_data = cast(dict[str, Any], _sanitize(event_data) or {})
        raw_meta = event_data.get("meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        resolved_snapshot = await self._optional_envelope(snapshot_ref)
        resolved_artifacts = [
            envelope for item in artifact_refs or [] if (envelope := await self._optional_envelope(item)) is not None
        ]
        async with self._lock:
            with self._connect(write=True) as connection:
                self._create_runtime_events_table(connection)
                if idempotency_key is not None:
                    duplicate = connection.execute(
                        "SELECT * FROM runtime_events WHERE execution_id = ? AND idempotency_key = ?",
                        (execution_id, idempotency_key),
                    ).fetchone()
                    if duplicate is not None:
                        return self._row_to_runtime_event(duplicate)
                current = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS value FROM runtime_events WHERE execution_id = ?",
                    (execution_id,),
                ).fetchone()
                next_sequence = int(current["value"]) + 1
                if expected_sequence is not None and int(expected_sequence) != next_sequence:
                    raise RuntimeError(
                        f"RecordStore runtime event sequence conflict for execution '{execution_id}': "
                        f"expected {expected_sequence}, next sequence is {next_sequence}."
                    )
                resolved_sequence = int(sequence) if sequence is not None else next_sequence
                if resolved_sequence != next_sequence:
                    raise RuntimeError(
                        f"RecordStore runtime event sequence conflict for execution '{execution_id}': "
                        f"received {resolved_sequence}, next sequence is {next_sequence}."
                    )
                created_at = _now()
                persisted_at = _now()
                event_id = str(event_data.get("event_id") or uuid.uuid4().hex)
                record_id = f"evt_{uuid.uuid4().hex}"
                values = (
                    record_id,
                    execution_id,
                    resolved_sequence,
                    event_id,
                    str(event_data.get("event_type") or "runtime.event"),
                    state_version,
                    idempotency_key,
                    parent_id or meta.get("parent_event_id") or meta.get("parent_id"),
                    causation_id or meta.get("causation_id"),
                    parent_signal_id or meta.get("parent_signal_id"),
                    node_id or meta.get("node_id"),
                    operator_id or meta.get("operator_id"),
                    interrupt_id or meta.get("interrupt_id"),
                    resume_request_id or meta.get("resume_request_id"),
                    actor_id or meta.get("actor_id"),
                    lease_owner_id or meta.get("lease_owner_id"),
                    aggregation_scope or meta.get("aggregation_scope"),
                    _json(resolved_snapshot) if resolved_snapshot is not None else None,
                    exchange_id,
                    _json(resolved_artifacts),
                    _json(event_data),
                    created_at,
                    persisted_at,
                )
                connection.execute(
                    "INSERT INTO runtime_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
                connection.commit()
                row = connection.execute("SELECT * FROM runtime_events WHERE id = ?", (record_id,)).fetchone()
        self._materialized_components.add("runtime_events")
        return self._row_to_runtime_event(cast(sqlite3.Row, row))

    async def query_runtime_events(
        self,
        execution_id: str,
        *,
        sequence_from: int | None = None,
        sequence_to: int | None = None,
        event_id: str | None = None,
        limit: int | None = None,
    ) -> list[StoredRuntimeEvent]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative.")
        if not self.db_path.exists():
            return []
        with self._connect() as connection:
            if not self._table_exists(connection, "runtime_events"):
                return []
            query = "SELECT * FROM runtime_events WHERE execution_id = ?"
            params: list[Any] = [execution_id]
            if sequence_from is not None:
                query += " AND sequence >= ?"
                params.append(sequence_from)
            if sequence_to is not None:
                query += " AND sequence <= ?"
                params.append(sequence_to)
            if event_id is not None:
                query += " AND event_id = ?"
                params.append(event_id)
            query += " ORDER BY sequence"
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_runtime_event(row) for row in rows]

    def capabilities(self) -> RecordStoreCapabilities:
        return {
            "root": str(self.root),
            "mode": "read_only" if self.read_only else "read_write",
            "external_read": False,
            "external_write": False,
            "private_write": not self.read_only,
            "materialized_components": sorted(self._materialized_components),
        }


__all__ = ["LocalRecordStore"]
