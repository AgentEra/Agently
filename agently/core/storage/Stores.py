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

import importlib
import inspect
import math
import sqlite3
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, cast

from agently.types.data.record_store import RecordRef, RerankScore

from .Errors import RecordStoreConfigurationError, RecordStorePolicyError
from ._utils import json_dumps, json_loads


VectorSimilarity = Literal["cosine", "dot", "l2"]
EmbeddingFunction = Callable[
    [str | list[str]],
    Sequence[float] | Sequence[Sequence[float]] | Awaitable[Sequence[float] | Sequence[Sequence[float]]],
]


class EmbeddingProviderUnavailableError(RuntimeError):
    """Raised when a vector pipeline has a vector store but no embedder."""


class VectorStoreProviderUnavailableError(RuntimeError):
    """Raised when a vector pipeline has an embedder but no vector store."""


def _coerce_embedding_vector(value: Sequence[float] | Sequence[Sequence[float]]) -> list[float]:
    if not value:
        return []
    first = value[0]
    if isinstance(first, Sequence) and not isinstance(first, (str, bytes, bytearray)):
        return [float(item) for item in first]
    return [float(item) for item in cast(Sequence[float], value)]


def _coerce_embedding_vectors(
    value: Sequence[float] | Sequence[Sequence[float]],
    expected_count: int,
) -> list[list[float]]:
    if expected_count <= 0:
        return []
    if not value:
        return [[] for _ in range(expected_count)]
    first = value[0]
    if isinstance(first, Sequence) and not isinstance(first, (str, bytes, bytearray)):
        vectors = [[float(item) for item in cast(Sequence[float], vector)] for vector in cast(Sequence[Sequence[float]], value)]
    else:
        vectors = [[float(item) for item in cast(Sequence[float], value)]]
    if len(vectors) < expected_count:
        vectors.extend([[] for _ in range(expected_count - len(vectors))])
    return vectors[:expected_count]


async def _maybe_await_embedding_result(
    result: Sequence[float] | Sequence[Sequence[float]] | Awaitable[Sequence[float] | Sequence[Sequence[float]]],
) -> Sequence[float] | Sequence[Sequence[float]]:
    if inspect.isawaitable(result):
        return await cast(Awaitable[Sequence[float] | Sequence[Sequence[float]]], result)
    return cast(Sequence[float] | Sequence[Sequence[float]], result)


class CallableEmbeddingProvider:
    name = "callable"

    def __init__(self, embedding_function: EmbeddingFunction):
        self.embedding_function = embedding_function

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        try:
            result = self.embedding_function(texts)
        except TypeError:
            if len(texts) != 1:
                vectors = []
                for text in texts:
                    single = await _maybe_await_embedding_result(self.embedding_function(text))
                    vectors.append(_coerce_embedding_vector(single))
                return vectors
            result = self.embedding_function(texts[0])
        resolved = await _maybe_await_embedding_result(result)
        return _coerce_embedding_vectors(cast(Sequence[float] | Sequence[Sequence[float]], resolved), len(texts))


class AgentEmbeddingProvider:
    name = "agent"

    def __init__(self, agent: Any):
        self.agent = agent

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        execution = self.agent.input(texts)
        async_start = getattr(execution, "async_start", None)
        if callable(async_start):
            result = async_start()
            resolved = await result if inspect.isawaitable(result) else result
        else:
            result = execution.start()
            resolved = await result if inspect.isawaitable(result) else result
        embedding_result = cast(Sequence[float] | Sequence[Sequence[float]], resolved)
        return _coerce_embedding_vectors(embedding_result, len(texts))


def _coerce_rerank_scores(value: Any, document_count: int) -> list[RerankScore]:
    """Normalize a provider reply into index/score pairs owned by the caller's order.

    Accepts scored objects (``index`` plus ``score`` or ``relevance_score``) and
    plain aligned number lists. Out-of-range and duplicate indexes are dropped
    instead of being remapped, so a candidate is never silently mismatched.
    """
    if value is None:
        return []
    if isinstance(value, Mapping):
        value = value.get("results", value.get("data", value.get("scores")))
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    scores: list[RerankScore] = []
    seen: set[int] = set()
    for position, item in enumerate(value):
        if isinstance(item, Mapping):
            raw_index = item.get("index", position)
            raw_score = item.get("score", item.get("relevance_score"))
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            raw_index = position
            raw_score = item
        else:
            continue
        try:
            index = int(raw_index)
            score = float(raw_score)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if index < 0 or index >= document_count or index in seen:
            continue
        if not math.isfinite(score):
            continue
        seen.add(index)
        scores.append({"index": index, "score": score})
    return scores


class CallableRerankProvider:
    """Wraps a caller-owned scoring function; sync and async callables both work."""

    name = "callable"

    def __init__(self, rerank_function: Callable[..., Any]):
        self.rerank_function = rerank_function

    async def rank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
    ) -> list[RerankScore]:
        try:
            result = self.rerank_function(query, documents, top_n=top_n)
        except TypeError:
            result = self.rerank_function(query, documents)
        if inspect.isawaitable(result):
            result = await result
        return _coerce_rerank_scores(result, len(documents))


class AgentRerankProvider:
    """Wraps an Agently Agent configured with ``model_type="rerank"``.

    ``top_n`` is deliberately not forwarded: the framework scores the whole
    bounded candidate window and orders it locally, so a truncated provider
    reply cannot starve candidates the retrieval layer still has to place.
    """

    name = "agent"

    def __init__(self, agent: Any):
        self.agent = agent

    async def rank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
    ) -> list[RerankScore]:
        _ = top_n
        execution = self.agent.input({"query": query, "documents": list(documents)})
        async_start = getattr(execution, "async_start", None)
        if callable(async_start):
            result = async_start()
        else:
            result = execution.start()
        resolved = await result if inspect.isawaitable(result) else result
        return _coerce_rerank_scores(resolved, len(documents))


class NoopVectorIndex:
    name = "noop"

    async def index_record(self, ref: RecordRef, content: str) -> None:
        _ = (ref, content)

    async def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[RecordRef]:
        _ = (query, filters, limit)
        return []


class VectorIndexPipeline:
    name = "vector_pipeline"

    def __init__(
        self,
        *,
        embedding_provider: Any | None,
        vector_store_provider: Any | None,
    ):
        self.embedding_provider = embedding_provider
        self.vector_store_provider = vector_store_provider
        self.similarity = getattr(vector_store_provider, "similarity", None)

    async def index_record(self, ref: RecordRef, content: str) -> None:
        if self.vector_store_provider is None:
            raise VectorStoreProviderUnavailableError("RecordStore vector store is unavailable.")
        if self.embedding_provider is None:
            return
        embeddings = await self.embedding_provider.embed_texts([content])
        embedding = embeddings[0] if embeddings else []
        if embedding:
            await self.vector_store_provider.index_record(ref, embedding)

    async def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[RecordRef]:
        if self.vector_store_provider is None:
            raise VectorStoreProviderUnavailableError("RecordStore vector store is unavailable.")
        if self.embedding_provider is None:
            return []
        embeddings = await self.embedding_provider.embed_texts([query])
        embedding = embeddings[0] if embeddings else []
        if not embedding:
            return []
        return await self.vector_store_provider.search_by_embedding(embedding, filters=filters, limit=limit)


class LocalVectorIndex:
    name = "local_vector"

    def __init__(
        self,
        embedding_function: EmbeddingFunction,
        *,
        similarity: VectorSimilarity = "cosine",
    ):
        self.embedding_function = embedding_function
        self.similarity: VectorSimilarity = similarity if similarity in {"cosine", "dot", "l2"} else "cosine"
        self._records: dict[str, tuple[RecordRef, list[float]]] = {}

    async def index_record(self, ref: RecordRef, content: str) -> None:
        vector = await self._embed_one(content)
        if vector:
            self._records[ref["id"]] = (ref, vector)

    async def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[RecordRef]:
        query_vector = await self._embed_one(query)
        if not query_vector:
            return []
        scored: list[tuple[float, RecordRef]] = []
        for ref, vector in self._records.values():
            if not self._matches_filters(ref, filters or {}):
                continue
            scored.append((self._score(query_vector, vector), ref))
        scored.sort(key=lambda item: item[0], reverse=True)
        if limit is not None:
            scored = scored[: max(0, limit)]
        return [ref for _, ref in scored]

    async def _embed_one(self, text: str) -> list[float]:
        try:
            result = self.embedding_function([text])
        except TypeError:
            result = self.embedding_function(text)
        if inspect.isawaitable(result):
            result = await cast(Awaitable[Sequence[float] | Sequence[Sequence[float]]], result)
        return self._coerce_one_vector(result)

    def _coerce_one_vector(self, value: Sequence[float] | Sequence[Sequence[float]]) -> list[float]:
        if not value:
            return []
        first = value[0]
        if isinstance(first, Sequence) and not isinstance(first, (str, bytes)):
            return [float(item) for item in first]
        return [float(item) for item in cast(Sequence[float], value)]

    def _score(self, left: Sequence[float], right: Sequence[float]) -> float:
        size = min(len(left), len(right))
        if size == 0:
            return float("-inf")
        left_values = [float(value) for value in left[:size]]
        right_values = [float(value) for value in right[:size]]
        if self.similarity == "dot":
            return sum(a * b for a, b in zip(left_values, right_values))
        if self.similarity == "l2":
            return -math.sqrt(sum((a - b) ** 2 for a, b in zip(left_values, right_values)))
        left_norm = math.sqrt(sum(value * value for value in left_values))
        right_norm = math.sqrt(sum(value * value for value in right_values))
        if left_norm == 0 or right_norm == 0:
            return float("-inf")
        return sum(a * b for a, b in zip(left_values, right_values)) / (left_norm * right_norm)

    def _matches_filters(self, ref: RecordRef, filters: Mapping[str, Any]) -> bool:
        for key, expected in filters.items():
            actual = self._resolve_ref_filter_value(ref, str(key))
            if isinstance(expected, (list, tuple, set, frozenset)):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    def _resolve_ref_filter_value(self, ref: RecordRef, key: str) -> Any:
        if key.startswith("scope."):
            return self._mapping_dot_get(ref.get("scope") or {}, key.removeprefix("scope."))
        if key.startswith("meta."):
            return self._mapping_dot_get(ref.get("meta") or {}, key.removeprefix("meta."))
        return ref.get(key)

    def _mapping_dot_get(self, mapping: Mapping[str, Any], key: str) -> Any:
        current: Any = mapping
        for part in key.split("."):
            if not isinstance(current, Mapping) or part not in current:
                return None
            current = current[part]
        return current


class SQLiteVectorStoreProvider:
    name = "sqlite"

    def __init__(
        self,
        db_path: str | Path,
        *,
        read_only: bool = False,
        create: bool = True,
        similarity: VectorSimilarity = "cosine",
    ):
        self.db_path = Path(db_path).expanduser().resolve()
        self.read_only = read_only
        self.similarity: VectorSimilarity = similarity if similarity in {"cosine", "dot", "l2"} else "cosine"
        if create and not read_only:
            self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS record_store_vectors (
                    record_id TEXT PRIMARY KEY,
                    collection TEXT NOT NULL,
                    kind TEXT,
                    path TEXT,
                    scope_json TEXT NOT NULL DEFAULT '{}',
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    ref_json TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS record_store_vectors_collection_kind_idx
                ON record_store_vectors(collection, kind, record_id)
                """
            )
            conn.commit()

    async def index_record(self, ref: RecordRef, embedding: list[float]) -> None:
        if self.read_only:
            raise RecordStorePolicyError("RecordStore vector store is configured read-only.")
        vector = [float(value) for value in embedding]
        if not vector:
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO record_store_vectors (
                    record_id, collection, kind, path, scope_json, meta_json,
                    ref_json, embedding_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ref["id"],
                    ref["collection"],
                    ref["kind"],
                    ref["path"],
                    json_dumps(ref.get("scope") or {}),
                    json_dumps(ref.get("meta") or {}),
                    json_dumps(ref),
                    json_dumps(vector),
                    ref["created_at"],
                ),
            )
            conn.commit()

    async def search_by_embedding(
        self,
        embedding: list[float],
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[RecordRef]:
        filters = filters or {}
        clauses: list[str] = []
        params: list[Any] = []
        if filters.get("id") is not None:
            clauses.append("record_id = ?")
            params.append(str(filters["id"]))
        if filters.get("path") is not None:
            clauses.append("path = ?")
            params.append(str(filters["path"]))
        if filters.get("collection") is not None:
            clauses.append("collection = ?")
            params.append(str(filters["collection"]))
        if filters.get("kind") is not None:
            clauses.append("kind = ?")
            params.append(str(filters["kind"]))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM record_store_vectors { where }", params).fetchall()
        query_vector = [float(value) for value in embedding]
        scored: list[tuple[float, RecordRef]] = []
        for row in rows:
            ref = cast(RecordRef, json_loads(row["ref_json"], {}))
            if not self._matches_filters(ref, filters):
                continue
            vector = [float(value) for value in json_loads(row["embedding_json"], [])]
            scored.append((self._score(query_vector, vector), ref))
        scored.sort(key=lambda item: item[0], reverse=True)
        if limit is not None:
            scored = scored[: max(0, limit)]
        return [ref for _, ref in scored]

    async def delete_records(self, record_ids: Sequence[str]) -> None:
        if not record_ids:
            return
        if self.read_only:
            raise RecordStorePolicyError("RecordStore vector store is configured read-only.")
        placeholders = ",".join("?" for _ in record_ids)
        with self._connect() as conn:
            conn.execute(f"DELETE FROM record_store_vectors WHERE record_id IN ({placeholders})", list(record_ids))
            conn.commit()

    def _score(self, left: Sequence[float], right: Sequence[float]) -> float:
        return _vector_score(left, right, self.similarity)

    def _matches_filters(self, ref: RecordRef, filters: Mapping[str, Any]) -> bool:
        return _record_matches_filters(ref, filters)


class ChromaVectorStoreProvider:
    name = "chroma"

    def __init__(
        self,
        root: str | Path,
        *,
        collection_name: str = "record_store_records",
        create: bool = True,
        mode: str = "read_write",
        similarity: VectorSimilarity = "cosine",
        client: Any | None = None,
    ):
        _ = create
        self.root = Path(root).expanduser().resolve()
        self.read_only = mode in {"read", "read_only", "readonly"}
        self.similarity: VectorSimilarity = similarity if similarity in {"cosine", "dot", "l2"} else "cosine"
        self.collection_name = collection_name
        try:
            chromadb = importlib.import_module("chromadb")
        except ImportError as error:
            raise RecordStoreConfigurationError("ChromaDB is not installed for RecordStore vector store.") from error
        self.root.mkdir(parents=True, exist_ok=True)
        self._client = client or chromadb.PersistentClient(path=str(self.root))
        metadata = {"hnsw:space": "ip" if self.similarity == "dot" else self.similarity}
        self._collection = self._client.get_or_create_collection(name=collection_name, metadata=metadata)

    async def index_record(self, ref: RecordRef, embedding: list[float]) -> None:
        if self.read_only:
            raise RecordStorePolicyError("RecordStore vector store is configured read-only.")
        vector = [float(value) for value in embedding]
        if not vector:
            return
        metadata = {
            "record_id": ref["id"],
            "collection": ref["collection"],
            "kind": ref.get("kind") or "",
            "path": ref.get("path") or "",
            "scope_json": json_dumps(ref.get("scope") or {}),
            "meta_json": json_dumps(ref.get("meta") or {}),
            "ref_json": json_dumps(ref),
        }
        self._collection.upsert(
            ids=[ref["id"]],
            embeddings=[vector],
            documents=[ref.get("summary") or ""],
            metadatas=[metadata],
        )

    async def search_by_embedding(
        self,
        embedding: list[float],
        *,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
    ) -> list[RecordRef]:
        filters = filters or {}
        safe_limit = max(1, int(limit or 10))
        n_results = max(safe_limit, 50 if filters else safe_limit)
        results = self._collection.query(query_embeddings=[[float(value) for value in embedding]], n_results=n_results)
        metadatas = (results.get("metadatas") or [[]])[0] if isinstance(results, dict) else []
        refs: list[RecordRef] = []
        for metadata in metadatas:
            if not isinstance(metadata, Mapping):
                continue
            ref = cast(RecordRef, json_loads(str(metadata.get("ref_json") or "{}"), {}))
            if not ref or not _record_matches_filters(ref, filters):
                continue
            refs.append(ref)
            if len(refs) >= safe_limit:
                break
        return refs

    async def delete_records(self, record_ids: Sequence[str]) -> None:
        if not record_ids:
            return
        if self.read_only:
            raise RecordStorePolicyError("RecordStore vector store is configured read-only.")
        self._collection.delete(ids=[str(record_id) for record_id in record_ids])


def _vector_score(left: Sequence[float], right: Sequence[float], similarity: VectorSimilarity) -> float:
    size = min(len(left), len(right))
    if size == 0:
        return float("-inf")
    left_values = [float(value) for value in left[:size]]
    right_values = [float(value) for value in right[:size]]
    if similarity == "dot":
        return sum(a * b for a, b in zip(left_values, right_values))
    if similarity == "l2":
        return -math.sqrt(sum((a - b) ** 2 for a, b in zip(left_values, right_values)))
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if left_norm == 0 or right_norm == 0:
        return float("-inf")
    return sum(a * b for a, b in zip(left_values, right_values)) / (left_norm * right_norm)


def _record_matches_filters(ref: RecordRef, filters: Mapping[str, Any]) -> bool:
    for key, expected in filters.items():
        actual = _resolve_ref_filter_value(ref, str(key))
        if isinstance(expected, (list, tuple, set, frozenset)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _resolve_ref_filter_value(ref: RecordRef, key: str) -> Any:
    if key.startswith("scope."):
        return _mapping_dot_get(ref.get("scope") or {}, key.removeprefix("scope."))
    if key.startswith("meta."):
        return _mapping_dot_get(ref.get("meta") or {}, key.removeprefix("meta."))
    return ref.get(key)


def _mapping_dot_get(mapping: Mapping[str, Any], key: str) -> Any:
    current: Any = mapping
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current
