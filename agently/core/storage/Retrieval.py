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

import inspect
import json
import math
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast

from agently.types.data.record_store import (
    RecordRef,
    RecordRetrievalItem,
    RecordRetrievalMethod,
    RecordRetrievalPackage,
    RecordRetrievalSelection,
)
from .Stores import (
    EmbeddingProviderUnavailableError,
    VectorStoreProviderUnavailableError,
    _coerce_rerank_scores,
)

if TYPE_CHECKING:
    from .RecordStore import RecordStore


RecordRepresentationMode = Literal["auto", "raw", "projected"]
RerankHandler = Callable[..., Any]


async def retrieve_records(
    record_store: "RecordStore",
    query: str | None = None,
    *,
    tags: Sequence[str] | None = None,
    filters: Mapping[str, Any] | None = None,
    scope: Mapping[str, Any] | None = None,
    budget: Mapping[str, Any] | None = None,
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
    normalized_budget = dict(budget or {})
    safe_max_candidates = _max_candidates(max_candidates, normalized_budget)
    scoped_filters = _scoped_retrieval_filters(filters, scope)
    normalized_tags = _normalize_tags(tags)
    requested_method, effective_method, method_resolution = _resolve_retrieval_method(
        record_store,
        method=method,
        settings=settings,
    )
    diagnostics: dict[str, Any] = {
        "profile": profile,
        "method": requested_method,
        "effective_method": effective_method,
        "method_resolution": method_resolution,
        "selection": selection,
        "sources": ["records"],
        "filters": scoped_filters,
        "tags": normalized_tags,
        "max_candidates": safe_max_candidates,
    }

    candidates = await _record_candidates(
        record_store,
        query=query,
        tags=normalized_tags,
        filters=scoped_filters,
        method=effective_method,
        max_candidates=safe_max_candidates,
        diagnostics=diagnostics,
    )

    candidates = _dedupe_candidates(candidates)[:safe_max_candidates]
    diagnostics["candidate_count"] = len(candidates)

    rerank_gate = _default_rerank_gate(
        candidates,
        sources={"records"},
        filters=scoped_filters,
        tags=normalized_tags,
        selection=selection,
        top_n=top_n,
        budget=normalized_budget,
    )
    diagnostics["rerank_gate"] = rerank_gate
    should_rerank = bool(rerank) if rerank is not None else bool(rerank_gate["enabled"])
    ranked_candidates = candidates
    dropped_by_rerank = 0
    if should_rerank and candidates:
        ranked_candidates, rerank_diagnostics, dropped_by_rerank = await _rerank_with_degrade(
            query=query,
            candidates=candidates,
            budget=normalized_budget,
            rerank_handler=rerank_handler,
            rerank_provider=_bound_rerank_provider(record_store) if rerank_handler is None else None,
            max_retries=max(0, int(max_rerank_retries)),
            plugin_manager=plugin_manager,
            settings=settings,
        )
        diagnostics["rerank"] = rerank_diagnostics
    else:
        diagnostics["rerank"] = {
            "enabled": False,
            "reason": (
                "disabled"
                if rerank is False
                else "no_candidates"
                if not candidates
                else str(rerank_gate.get("reason") or "gate_skipped")
            ),
        }

    package, budget_omitted = await _package_candidates(
        record_store,
        ranked_candidates,
        query=query,
        profile=profile,
        selection=selection,
        top_n=top_n,
        budget=normalized_budget,
        diagnostics=diagnostics,
    )
    omitted = list(package["omitted"])
    if dropped_by_rerank:
        omitted.insert(0, {"reason": "rerank_drop", "count": dropped_by_rerank})
    if budget_omitted:
        omitted.append({"reason": "budget", "count": budget_omitted})
    package["omitted"] = omitted
    package["diagnostics"] = diagnostics
    return package


def _max_candidates(max_candidates: int | None, budget: Mapping[str, Any]) -> int:
    configured = max_candidates if max_candidates is not None else budget.get("max_candidates")
    if isinstance(configured, int):
        return max(1, min(configured, 500))
    return 50


def _scoped_retrieval_filters(
    filters: Mapping[str, Any] | None,
    scope: Mapping[str, Any] | None,
) -> dict[str, Any]:
    scoped = dict(filters or {})
    for key, value in dict(scope or {}).items():
        filter_key = str(key) if str(key).startswith("scope.") else f"scope.{ key }"
        scoped.setdefault(filter_key, value)
    return scoped


def _normalize_tags(tags: Sequence[str] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        normalized = str(tag).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _resolve_retrieval_method(
    record_store: "RecordStore",
    *,
    method: RecordRetrievalMethod | str | None,
    settings: Any,
) -> tuple[RecordRetrievalMethod, Literal["keyword", "vector", "hybrid"], dict[str, Any]]:
    requested = str(method or "auto").strip().lower()
    if requested not in {"auto", "keyword", "vector", "hybrid"}:
        requested = "auto"
    requested_method = cast(RecordRetrievalMethod, requested)
    if requested in {"keyword", "vector", "hybrid"}:
        return requested_method, cast(Literal["keyword", "vector", "hybrid"], requested), {
            "mode": "explicit",
            "reason": "caller_requested",
        }

    vector_available, vector_unavailable_reason = _record_store_vector_state(record_store)
    embedding_policy_configured, embedding_policy_reason = _embedding_policy_configured(settings)
    if vector_available and embedding_policy_configured:
        return requested_method, "hybrid", {
            "mode": "auto",
            "reason": embedding_policy_reason,
            "vector_index_available": True,
        }
    return requested_method, "keyword", {
        "mode": "auto",
        "reason": vector_unavailable_reason if not vector_available else "embedding_policy_not_configured",
        "vector_index_available": vector_available,
    }


def _record_store_vector_available(record_store: "RecordStore") -> bool:
    available, _ = _record_store_vector_state(record_store)
    return available


def _record_store_vector_state(record_store: "RecordStore") -> tuple[bool, str]:
    backend = getattr(record_store, "backend", None)
    if getattr(backend, "vector_store_fallback_reason", None):
        fallback_reason = str(getattr(backend, "vector_store_fallback_reason"))
    else:
        fallback_reason = ""
    vector_index = getattr(getattr(record_store, "backend", None), "vector_index", None)
    if vector_index is None or getattr(vector_index, "name", None) == "noop":
        return False, "vector_store_unavailable"
    if getattr(vector_index, "name", None) == "vector_pipeline":
        if getattr(backend, "vector_store_provider", None) is None:
            return False, "vector_store_unavailable"
        if getattr(backend, "embedding_provider", None) is None:
            return False, "embedding_provider_unavailable"
    return True, fallback_reason or "available"


def _embedding_policy_configured(settings: Any) -> tuple[bool, str]:
    candidate_strategy = _settings_get_first(
        settings,
        "record_store.retrieval.candidate_strategy",
        "record_store.retrieve.candidate_strategy",
    )
    if str(candidate_strategy or "").strip().lower() in {"hybrid", "vector"}:
        return True, "candidate_strategy"
    default_method = _settings_get_first(
        settings,
        "record_store.retrieval.default_method",
        "record_store.retrieve.default_method",
    )
    if str(default_method or "").strip().lower() in {"hybrid", "vector"}:
        return True, "default_method"
    if _settings_bool(
        settings,
        "record_store.retrieval.vector_preferred",
        "record_store.retrieval.embeddings.enabled",
        "record_store.embeddings.enabled",
    ):
        return True, "vector_preferred"
    embedding_model = _settings_get_first(
        settings,
        "record_store.retrieval.embedding_model",
        "record_store.retrieval.embeddings.model",
        "record_store.embeddings.model",
        "embeddings.model",
    )
    if isinstance(embedding_model, str) and embedding_model.strip():
        return True, "embedding_model"
    return False, "embedding_policy_not_configured"


def _settings_bool(settings: Any, *keys: str) -> bool:
    value = _settings_get_first(settings, *keys)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _settings_get_first(settings: Any, *keys: str) -> Any:
    sources: list[Any] = [settings] if settings is not None else []
    if settings is None:
        try:
            from agently.base import settings as global_settings

            sources.append(global_settings)
        except Exception:
            pass
    for source in sources:
        for key in keys:
            try:
                value = source.get(key, None)
            except Exception:
                value = None
            if value is None and isinstance(source, Mapping):
                value = _mapping_get_dot_path(source, key)
            if value is not None:
                return value
    return None


def _mapping_get_dot_path(mapping: Mapping[str, Any], key: str) -> Any:
    current: Any = mapping
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


async def _record_candidates(
    record_store: "RecordStore",
    *,
    query: str | None,
    tags: list[str],
    filters: dict[str, Any],
    method: RecordRetrievalMethod,
    max_candidates: int,
    diagnostics: dict[str, Any],
) -> list[dict[str, Any]]:
    deterministic = await _deterministic_record_candidates(
        record_store,
        query=query,
        tags=tags,
        filters=filters,
        max_candidates=max_candidates,
    )
    diagnostics["deterministic_record_candidates"] = len(deterministic)
    if method not in {"vector", "hybrid"}:
        return deterministic

    vector_records = await _vector_record_candidates(
        record_store,
        query=query,
        tags=tags,
        filters=filters,
        limit=max_candidates,
        diagnostics=diagnostics,
    )
    if method == "vector" and vector_records:
        return vector_records + deterministic
    return vector_records + deterministic


async def _deterministic_record_candidates(
    record_store: "RecordStore",
    *,
    query: str | None,
    tags: list[str],
    filters: dict[str, Any],
    max_candidates: int,
) -> list[dict[str, Any]]:
    by_id: dict[str, RecordRef] = {}
    ordered: list[RecordRef] = []

    async def add_records(records: list[RecordRef], *, require_tags: bool) -> None:
        for record in records:
            if require_tags and not _record_matches_tags(record, tags):
                continue
            record_id = record["id"]
            if record_id in by_id:
                continue
            by_id[record_id] = record
            ordered.append(record)

    if query:
        await add_records(await record_store.grep(query, filters=filters), require_tags=bool(tags))
    if tags:
        await add_records(await record_store.grep(None, filters=filters), require_tags=True)
    if not ordered:
        await add_records(await record_store.grep(None, filters=filters), require_tags=False)

    return [_record_candidate(record) for record in ordered[:max_candidates]]


async def _vector_record_candidates(
    record_store: "RecordStore",
    *,
    query: str | None,
    tags: list[str],
    filters: dict[str, Any],
    limit: int,
    diagnostics: dict[str, Any],
) -> list[dict[str, Any]]:
    vector_diagnostics: dict[str, Any] = {"requested": True, "used": False}
    diagnostics["vector"] = vector_diagnostics
    vector_index = getattr(record_store.backend, "vector_index", None)
    vector_available, unavailable_reason = _record_store_vector_state(record_store)
    backend = getattr(record_store, "backend", None)
    if getattr(backend, "vector_store_provider_name", None):
        vector_diagnostics["vector_store_provider"] = getattr(backend, "vector_store_provider_name")
    if getattr(backend, "vector_store_fallback_reason", None):
        vector_diagnostics["fallback_reason"] = getattr(backend, "vector_store_fallback_reason")
    if query is None or str(query).strip() == "":
        vector_diagnostics["reason"] = "empty_query"
        return []
    if not vector_available:
        vector_diagnostics["reason"] = unavailable_reason
        return []
    if vector_index is None:
        vector_diagnostics["reason"] = unavailable_reason or "vector_store_unavailable"
        return []
    similarity = getattr(vector_index, "similarity", None)
    if similarity is not None:
        vector_diagnostics["similarity"] = similarity
    try:
        records = await vector_index.search(str(query), filters=filters, limit=limit)
    except EmbeddingProviderUnavailableError as exc:
        vector_diagnostics["reason"] = "embedding_provider_unavailable"
        vector_diagnostics["error"] = str(exc)
        return []
    except VectorStoreProviderUnavailableError as exc:
        vector_diagnostics["reason"] = "vector_store_unavailable"
        vector_diagnostics["error"] = str(exc)
        return []
    except Exception as exc:
        vector_diagnostics["reason"] = "vector_search_failed"
        vector_diagnostics["error"] = str(exc)
        return []
    vector_diagnostics["used"] = True
    vector_diagnostics["candidate_count"] = len(records)
    return [_record_candidate(record) for record in records if _record_matches_tags(record, tags)]


def _record_candidate(record: RecordRef) -> dict[str, Any]:
    return {
        "source": "record",
        "candidate_id": f"record:{ record['id'] }",
        "ref": record,
        "tags": _record_tags(record),
        "score": None,
        "reason": None,
    }


def _record_tags(record: RecordRef) -> list[str]:
    tags: list[str] = []
    for holder in (record.get("meta"), record.get("scope")):
        if not isinstance(holder, dict):
            continue
        raw = holder.get("tags")
        if isinstance(raw, str):
            tags.append(raw)
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            tags.extend(str(tag) for tag in raw if str(tag).strip())
    return _normalize_tags(tags)


def _record_matches_tags(record: RecordRef, tags: list[str]) -> bool:
    if not tags:
        return True
    record_tags = set(_record_tags(record))
    return all(tag in record_tags for tag in tags)


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        if not candidate_id or candidate_id in seen:
            continue
        seen.add(candidate_id)
        result.append(candidate)
    return result


_PROJECTION_OMIT_KEYS = {
    "audit",
    "auth",
    "collection",
    "collection_hint",
    "export_batch",
    "format",
    "labels",
    "noise",
    "object_type",
    "priority_hint",
    "queue",
    "routing",
    "schema",
    "source_system",
    "source_tags",
    "tags",
}

_PLAIN_TEXT_KEYS = {
    "body",
    "content",
    "conversation",
    "description",
    "fact",
    "facts",
    "message",
    "note",
    "notes",
    "raw_lines",
    "snippet",
    "text",
}


def _default_rerank_gate(
    candidates: list[dict[str, Any]],
    *,
    sources: set[str],
    filters: Mapping[str, Any],
    tags: list[str],
    selection: RecordRetrievalSelection,
    top_n: int | None,
    budget: Mapping[str, Any],
) -> dict[str, Any]:
    candidate_count = len(candidates)
    target_count = _rerank_gate_target_count(selection=selection, top_n=top_n, budget=budget)
    profile = _candidate_pool_profile(candidates)
    strong_filters = _has_strong_retrieval_filters(filters=filters, tags=tags)
    reasons: list[str] = []
    if candidate_count == 0:
        return {
            "enabled": False,
            "reason": "no_candidates",
            "candidate_count": 0,
            "target_count": target_count,
            "strong_filters": strong_filters,
            **profile,
        }
    if candidate_count <= target_count:
        return {
            "enabled": False,
            "reason": "candidate_count_within_selection",
            "candidate_count": candidate_count,
            "target_count": target_count,
            "strong_filters": strong_filters,
            **profile,
        }
    if candidate_count >= max(8, target_count * 2):
        reasons.append("large_candidate_pool")
    if profile["source_count"] > 1:
        reasons.append("mixed_sources")
    if profile["collection_count"] > 1:
        reasons.append("mixed_collections")
    if profile["kind_count"] > 3:
        reasons.append("many_record_kinds")
    if profile["path_bucket_count"] > 3:
        reasons.append("many_record_paths")
    if not strong_filters:
        reasons.append("weak_structural_filters")
    enabled = bool(reasons)
    return {
        "enabled": enabled,
        "reason": "rerank_gate_matched" if enabled else "candidate_pool_structurally_focused",
        "reasons": reasons,
        "candidate_count": candidate_count,
        "target_count": target_count,
        "strong_filters": strong_filters,
        "selected_sources": sorted(sources),
        **profile,
    }


def _rerank_gate_target_count(
    *,
    selection: RecordRetrievalSelection,
    top_n: int | None,
    budget: Mapping[str, Any],
) -> int:
    for value in (top_n, budget.get("top_n")):
        if isinstance(value, int):
            return max(1, min(value, 50))
    if selection == "top_n":
        return 8
    return 5


def _candidate_pool_profile(candidates: list[dict[str, Any]]) -> dict[str, int]:
    sources: set[str] = set()
    collections: set[str] = set()
    kinds: set[str] = set()
    paths: set[str] = set()
    for candidate in candidates:
        source = str(candidate.get("source") or "")
        if source:
            sources.add(source)
        ref = candidate.get("ref")
        if isinstance(ref, Mapping):
            collection = str(ref.get("collection") or "")
            kind = str(ref.get("kind") or "")
            if collection:
                collections.add(collection)
            if kind:
                kinds.add(kind)
            path = str(ref.get("path") or "")
            if path:
                paths.add(path)
    return {
        "source_count": len(sources),
        "collection_count": len(collections),
        "kind_count": len(kinds),
        "path_bucket_count": len(paths),
    }


def _has_strong_retrieval_filters(
    *,
    filters: Mapping[str, Any],
    tags: list[str],
) -> bool:
    if tags:
        return True
    strong_filter_keys = {"id", "collection", "kind", "path"}
    for key, value in filters.items():
        if value in (None, "", [], {}):
            continue
        key_text = str(key)
        if key_text in strong_filter_keys or key_text.startswith("scope.") or key_text.startswith("meta."):
            return True
    return False


async def _rerank_with_degrade(
    *,
    query: str | None,
    candidates: list[dict[str, Any]],
    budget: Mapping[str, Any],
    rerank_handler: RerankHandler | None,
    rerank_provider: Any = None,
    max_retries: int,
    plugin_manager: Any,
    settings: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    rerank_limit = _rerank_limit(candidates, budget)
    rerank_candidates = candidates[:rerank_limit]
    remaining_candidates = candidates[rerank_limit:]
    engine = "handler" if rerank_handler is not None else "provider" if rerank_provider is not None else "model"
    diagnostics: dict[str, Any] = {
        "enabled": True,
        "engine": engine,
        "candidate_count": len(rerank_candidates),
        "attempts": 0,
    }
    if engine == "provider":
        diagnostics["provider"] = str(getattr(rerank_provider, "name", type(rerank_provider).__name__))
    last_error: str | None = None
    for attempt in range(max_retries + 1):
        diagnostics["attempts"] = attempt + 1
        try:
            if engine == "provider":
                ranked, dropped, provider_facts = await _provider_rerank(
                    query=query,
                    candidates=rerank_candidates,
                    provider=rerank_provider,
                    budget=budget,
                )
                diagnostics.update(provider_facts)
                diagnostics.update({"degraded": False, "dropped": dropped, "kept": len(ranked)})
                return ranked + remaining_candidates, diagnostics, dropped
            decisions = await _run_rerank_handler(
                query=query,
                candidates=rerank_candidates,
                rerank_handler=rerank_handler,
                plugin_manager=plugin_manager,
                settings=settings,
            )
            ranked, dropped = _apply_rerank_decisions(rerank_candidates, decisions)
            diagnostics.update(
                {
                    "degraded": False,
                    "dropped": dropped,
                    "kept": len(ranked),
                }
            )
            return ranked + remaining_candidates, diagnostics, dropped
        except Exception as exc:
            last_error = str(exc)
    diagnostics.update(
        {
            "degraded": True,
            "reason": "rerank_failed",
            "error": last_error,
            "dropped": 0,
        }
    )
    return candidates, diagnostics, 0


def _bound_rerank_provider(record_store: "RecordStore") -> Any:
    """Return the bound reranker, or None. Absence is ordinary, not a failure."""
    try:
        backend = record_store.backend
    except Exception:
        return None
    ensure = getattr(backend, "ensure_rerank_provider", None)
    if callable(ensure):
        try:
            provider = ensure()
        except Exception:
            return None
    else:
        provider = getattr(backend, "rerank_provider", None)
    return provider if callable(getattr(provider, "rank", None)) else None


def _candidate_document_text(candidate: dict[str, Any]) -> str:
    """Project one candidate into the same bounded material the model rerank sees."""
    preview = _candidate_preview(candidate)
    parts: list[str] = []
    for value in (preview.get("summary"), preview.get("kind"), preview.get("collection")):
        text = str(value or "").strip()
        if text:
            parts.append(text)
    tags = preview.get("tags")
    if isinstance(tags, list) and tags:
        parts.append(" ".join(str(tag) for tag in tags if str(tag).strip()))
    return "\n".join(parts) or str(preview.get("id") or "")


def _rerank_score_floor(budget: Mapping[str, Any]) -> float | None:
    raw = budget.get("rerank_score_floor")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    floor = float(raw)
    return floor if math.isfinite(floor) else None


async def _provider_rerank(
    *,
    query: str | None,
    candidates: list[dict[str, Any]],
    provider: Any,
    budget: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    """Order candidates by provider relevance score.

    A reranker model produces relevance, not a usefulness verdict, so nothing is
    dropped unless the caller sets an explicit `rerank_score_floor`. Candidates
    the provider did not score keep their original relative order behind the
    scored ones instead of being discarded on absent evidence.
    """
    documents = [_candidate_document_text(candidate) for candidate in candidates]
    raw_scores = await provider.rank(query or "", documents, top_n=None)
    scores = _coerce_rerank_scores(raw_scores, len(documents))
    if not scores:
        raise ValueError("Rerank provider returned no usable relevance scores.")
    score_by_index = {int(item["index"]): float(item["score"]) for item in scores}
    floor = _rerank_score_floor(budget)
    scored: list[tuple[float, int, dict[str, Any]]] = []
    unscored: list[dict[str, Any]] = []
    dropped = 0
    for index, candidate in enumerate(candidates):
        if index not in score_by_index:
            unscored.append(candidate)
            continue
        score = score_by_index[index]
        if floor is not None and score < floor:
            dropped += 1
            continue
        ranked_candidate = dict(candidate)
        ranked_candidate["score"] = score
        scored.append((score, index, ranked_candidate))
    scored.sort(key=lambda item: (-item[0], item[1]))
    facts: dict[str, Any] = {
        "scored": len(scored) + dropped,
        "unscored": len(unscored),
    }
    if floor is not None:
        facts["score_floor"] = floor
    return [item[2] for item in scored] + unscored, dropped, facts


def _rerank_limit(candidates: list[dict[str, Any]], budget: Mapping[str, Any]) -> int:
    configured = budget.get("rerank_candidates")
    if isinstance(configured, int):
        return max(1, min(len(candidates), configured))
    top_n = budget.get("top_n")
    if isinstance(top_n, int):
        return max(1, min(len(candidates), 30, max(top_n * 3, top_n + 10, 20)))
    return max(1, min(len(candidates), 20))


async def _run_rerank_handler(
    *,
    query: str | None,
    candidates: list[dict[str, Any]],
    rerank_handler: RerankHandler | None,
    plugin_manager: Any,
    settings: Any,
) -> Any:
    previews = [_candidate_preview(candidate) for candidate in candidates]
    if rerank_handler is not None:
        result = rerank_handler(query=query, candidates=previews)
        if inspect.isawaitable(result):
            return await result
        return result
    unavailable = _default_model_unavailable_reason(settings)
    if unavailable is not None:
        raise RuntimeError(unavailable)
    return await _default_model_rerank(query=query, candidates=previews, plugin_manager=plugin_manager, settings=settings)


def _candidate_preview(candidate: dict[str, Any]) -> dict[str, Any]:
    ref = cast(RecordRef, candidate["ref"])
    return {
        "id": candidate["candidate_id"],
        "source": "record",
        "collection": ref.get("collection"),
        "kind": ref.get("kind"),
        "summary": ref.get("summary") or "",
        "tags": candidate.get("tags") or [],
        "scope": ref.get("scope") or {},
    }


def _default_model_unavailable_reason(settings: Any) -> str | None:
    try:
        active = str(settings.get("plugins.ModelRequester.activate", "")) if settings is not None else ""
    except Exception:
        active = ""
    if not active:
        try:
            from agently.base import settings as global_settings

            settings = global_settings
            active = str(settings.get("plugins.ModelRequester.activate", ""))
        except Exception:
            return "No active ModelRequester is available for RecordStore retrieval rerank."
    if active in {"OpenAICompatible", "OpenAIResponsesCompatible"} and settings is not None:
        prefix = f"plugins.ModelRequester.{ active }"
        base_url = str(settings.get(f"{ prefix }.base_url", ""))
        full_url = settings.get(f"{ prefix }.full_url", None)
        api_key = settings.get(f"{ prefix }.api_key", None)
        auth = settings.get(f"{ prefix }.auth", None)
        if "api.openai.com" in base_url and full_url is None and not api_key and not auth:
            return "RecordStore retrieval rerank skipped because the active OpenAI-compatible model has no auth."
    return None


async def _default_model_rerank(
    *,
    query: str | None,
    candidates: list[dict[str, Any]],
    plugin_manager: Any,
    settings: Any,
) -> Any:
    if plugin_manager is None or settings is None:
        from agently.base import plugin_manager as global_plugin_manager
        from agently.base import settings as global_settings

        plugin_manager = plugin_manager or global_plugin_manager
        settings = settings or global_settings
    from agently.core.model import ModelRequest

    request = ModelRequest(
        plugin_manager,
        agent_name="RecordStoreRetrieval",
        parent_settings=settings,
    )
    request.input(
        {
            "query": query or "",
            "candidates": candidates,
        }
    )
    request.instruct(
        "Judge which RecordStore retrieval candidates are useful for the query. "
        "Drop only candidates that are not useful. Return structured decisions."
    )
    request.output(
        {
            "decisions": [
                {
                    "id": (str, "Candidate id exactly as provided."),
                    "useful": (bool, "True if the candidate should be kept."),
                    "score": (float, "Relevance score from 0.0 to 1.0."),
                    "reason": (str, "Short reason for the decision."),
                }
            ]
        },
        format="json",
    )
    return await request.async_get_data(max_retries=0, raise_ensure_failure=True)


def _apply_rerank_decisions(
    candidates: list[dict[str, Any]],
    decisions_payload: Any,
) -> tuple[list[dict[str, Any]], int]:
    decisions = _coerce_decisions(decisions_payload)
    if not decisions:
        raise ValueError("Rerank response did not contain decisions.")
    kept: list[dict[str, Any]] = []
    dropped = 0
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        decision = _decision_for_candidate(candidate_id, decisions)
        if decision is None:
            kept.append(candidate)
            continue
        if not bool(decision["useful"]):
            dropped += 1
            continue
        ranked = dict(candidate)
        ranked["score"] = decision.get("score")
        ranked["reason"] = decision.get("reason")
        kept.append(ranked)
    kept.sort(key=lambda item: (item.get("score") is not None, float(item.get("score") or 0.0)), reverse=True)
    return kept, dropped


def _decision_for_candidate(candidate_id: str, decisions: Mapping[str, dict[str, Any]]) -> dict[str, Any] | None:
    decision = decisions.get(candidate_id)
    if decision is not None:
        return decision
    if ":" in candidate_id:
        _, bare_id = candidate_id.split(":", 1)
        decision = decisions.get(bare_id)
        if decision is not None:
            return decision
    for prefix in ("record:", "file:"):
        decision = decisions.get(f"{prefix}{candidate_id}")
        if decision is not None:
            return decision
    return None


def _coerce_decisions(payload: Any) -> dict[str, dict[str, Any]]:
    raw_decisions: Any
    if isinstance(payload, Mapping):
        raw_decisions = payload.get("decisions", payload)
    else:
        raw_decisions = payload
    decisions: dict[str, dict[str, Any]] = {}
    if isinstance(raw_decisions, Mapping):
        for key, value in raw_decisions.items():
            decisions[str(key)] = _coerce_decision_value(value)
        return decisions
    if isinstance(raw_decisions, Sequence) and not isinstance(raw_decisions, (str, bytes, bytearray)):
        for item in raw_decisions:
            if not isinstance(item, Mapping):
                continue
            candidate_id = item.get("id") or item.get("candidate_id")
            if candidate_id is None:
                continue
            decisions[str(candidate_id)] = _coerce_decision_value(item)
    return decisions


def _coerce_decision_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"useful": value, "score": 1.0 if value else 0.0, "reason": None}
    if isinstance(value, Mapping):
        raw_useful = value.get("useful", value.get("keep", value.get("decision", True)))
        useful = _coerce_useful(raw_useful)
        return {
            "useful": useful,
            "score": _coerce_score(value.get("score", 1.0 if useful else 0.0)),
            "reason": value.get("reason"),
        }
    return {"useful": True, "score": None, "reason": None}


def _coerce_useful(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"drop", "dropped", "discard", "false", "no", "irrelevant", "not_useful"}:
        return False
    return True


def _coerce_score(value: Any) -> float | None:
    try:
        score = float(value)
    except Exception:
        return None
    return max(0.0, min(1.0, score))


async def _package_candidates(
    record_store: "RecordStore",
    candidates: list[dict[str, Any]],
    *,
    query: str | None,
    profile: str,
    selection: RecordRetrievalSelection,
    top_n: int | None,
    budget: Mapping[str, Any],
    diagnostics: dict[str, Any],
) -> tuple[RecordRetrievalPackage, int]:
    char_budget = _char_budget(budget)
    item_char_budget = _item_char_budget(budget, char_budget)
    record_representation = _record_representation_mode(budget)
    selected: list[RecordRetrievalItem] = []
    omitted_budget = 0
    used_chars = 0
    representation_counts: dict[str, int] = {}
    top_n_limit = top_n if top_n is not None else budget.get("top_n")
    if isinstance(top_n_limit, int):
        top_n_limit = max(1, top_n_limit)
    else:
        top_n_limit = None

    for candidate in candidates:
        if selection == "top_n" and top_n_limit is not None and len(selected) >= top_n_limit:
            omitted_budget += 1
            continue
        item = await _candidate_item(
            record_store,
            candidate,
            item_char_budget,
            record_representation=record_representation,
        )
        item_chars = int(item.get("chars") or 0)
        if selection == "length" and selected and used_chars + item_chars > char_budget:
            omitted_budget += 1
            continue
        if selection == "length" and not selected and item_chars > char_budget:
            mutable_item = cast(RecordRetrievalItem, dict(item))
            content = item.get("content")
            if isinstance(content, str):
                summary_text = str(mutable_item.get("summary") or "")
                remaining = max(0, char_budget - len(summary_text))
                mutable_item["content"] = _excerpt(content, remaining)
                content_text = str(mutable_item.get("content") or "")
                mutable_item["chars"] = len(summary_text) + len(content_text)
                item_chars = int(mutable_item["chars"])
            item = mutable_item
        selected.append(item)
        used_chars += item_chars
        if item.get("source") == "record":
            projection = item.get("projection")
            representation = (
                str(projection.get("chosen_representation") or projection.get("strategy") or "unknown")
                if isinstance(projection, Mapping)
                else "unknown"
            )
            representation_counts[representation] = representation_counts.get(representation, 0) + 1

    diagnostics["used_chars"] = used_chars
    diagnostics["char_budget"] = char_budget
    diagnostics["selected_count"] = len(selected)
    diagnostics["representation"] = {
        "record_mode": record_representation,
        "record_counts": representation_counts,
    }
    return (
        {
            "query": query,
            "profile": profile,
            "selection": selection,
            "items": selected,
            "omitted": [],
            "diagnostics": diagnostics,
        },
        omitted_budget,
    )


async def _candidate_item(
    record_store: "RecordStore",
    candidate: dict[str, Any],
    item_char_budget: int,
    *,
    record_representation: RecordRepresentationMode,
) -> RecordRetrievalItem:
    return await _record_item(
        record_store,
        candidate,
        item_char_budget,
        record_representation=record_representation,
    )


async def _record_item(
    record_store: "RecordStore",
    candidate: dict[str, Any],
    item_char_budget: int,
    *,
    record_representation: RecordRepresentationMode,
) -> RecordRetrievalItem:
    ref = cast(RecordRef, candidate["ref"])
    value = await _safe_record_value(record_store, ref)
    projection = _project_record_value(value, item_char_budget, representation=record_representation)
    summary = ref.get("summary") or ""
    content = projection["content"]
    content_text = content or ""
    return {
        "source": "record",
        "candidate_id": str(candidate.get("candidate_id")),
        "ref": ref,
        "kind": ref.get("kind"),
        "summary": summary,
        "content": content,
        "tags": list(candidate.get("tags") or []),
        "score": candidate.get("score"),
        "reason": candidate.get("reason"),
        "use": "memory" if ref.get("collection") == "memory" else "context",
        "chars": len(summary) + len(content_text),
        "body_state": "truncated" if projection["truncated"] else "bounded",
        "content_state": projection["content_state"],
        "original_ref": _original_record_ref(ref),
        "projection": projection["metadata"],
        "raw_chars": int(projection["metadata"].get("raw_chars") or 0),
        "projected_chars": int(projection["metadata"].get("projected_chars") or len(content_text)),
        "truncated": bool(projection["truncated"]),
    }


async def _safe_record_value(record_store: "RecordStore", record: RecordRef) -> Any:
    try:
        return await record_store.get_data(record)
    except Exception:
        return None


def _record_representation_mode(budget: Mapping[str, Any]) -> RecordRepresentationMode:
    raw = budget.get("record_representation", budget.get("representation", None))
    if raw is not None:
        normalized = str(raw).strip().lower()
        if normalized in {"raw", "raw_excerpt", "off", "disabled", "false", "no", "0"}:
            return "raw"
        if normalized in {"projected", "projection", "project", "force_projected"}:
            return "projected"
        return "auto"
    raw = budget.get("record_projection", budget.get("projection", None))
    if isinstance(raw, bool):
        return "auto" if raw else "raw"
    if raw is None:
        return "auto"
    normalized = str(raw).strip().lower()
    if normalized in {"0", "false", "no", "off", "raw", "disabled"}:
        return "raw"
    if normalized in {"projected", "projection", "project", "force_projected"}:
        return "projected"
    return "auto"


def _project_record_value(
    value: Any,
    max_chars: int,
    *,
    representation: RecordRepresentationMode,
) -> dict[str, Any]:
    raw_text = _record_raw_text(value) or ""
    if representation == "raw" or not isinstance(value, (Mapping, Sequence)) or isinstance(value, (str, bytes, bytearray)):
        excerpt = _excerpt(raw_text, max_chars)
        return _projection_result(
            content=excerpt,
            raw_chars=len(raw_text),
            strategy="raw_excerpt",
            chosen_representation="raw",
            content_state="bounded_readback_available",
            truncated=bool(excerpt and excerpt.endswith("\n[truncated]")),
            omitted_keys=[],
            reason="forced_raw" if representation == "raw" else "non_structured_value",
        )
    projected_lines, omitted_keys = _record_projection_lines(value)
    projected_text = "\n".join(line for line in projected_lines if line.strip()).strip()
    compact_structured_text, compact_omitted_keys = _compact_structured_record_text(value, max_chars=max_chars)
    omitted_keys = [*omitted_keys, *compact_omitted_keys]
    if representation == "auto" and compact_structured_text:
        compact_limit = _auto_compact_structured_limit(max_chars)
        if len(compact_structured_text) <= compact_limit:
            excerpt = _excerpt(compact_structured_text, max_chars)
            return _projection_result(
                content=excerpt,
                raw_chars=len(raw_text),
                strategy="compact_structured_record",
                chosen_representation="compact_structured",
                content_state="compact_structured_record",
                truncated=bool(excerpt and excerpt.endswith("\n[truncated]")),
                omitted_keys=omitted_keys,
                reason="short_structured_record_preserved",
                projected_candidate_chars=len(projected_text),
                compact_structured_chars=len(compact_structured_text),
            )
    if not projected_text:
        excerpt = _excerpt(raw_text, max_chars)
        return _projection_result(
            content=excerpt,
            raw_chars=len(raw_text),
            strategy="raw_excerpt_empty_projection",
            chosen_representation="raw",
            content_state="bounded_readback_available",
            truncated=bool(excerpt and excerpt.endswith("\n[truncated]")),
            omitted_keys=omitted_keys,
            reason="empty_projection",
            compact_structured_chars=len(compact_structured_text),
        )
    if representation == "auto" and len(projected_text) >= len(raw_text):
        excerpt = _excerpt(raw_text, max_chars)
        return _projection_result(
            content=excerpt,
            raw_chars=len(raw_text),
            strategy="raw_excerpt_not_shorter",
            chosen_representation="raw",
            content_state="bounded_readback_available",
            truncated=bool(excerpt and excerpt.endswith("\n[truncated]")),
            omitted_keys=omitted_keys,
            reason="projection_not_shorter",
            projected_candidate_chars=len(projected_text),
            compact_structured_chars=len(compact_structured_text),
        )
    excerpt = _excerpt(projected_text, max_chars)
    return _projection_result(
        content=excerpt,
        raw_chars=len(raw_text),
        strategy="deterministic_structured_projection",
        chosen_representation="projected",
        content_state="projected_from_raw_record",
        truncated=bool(excerpt and excerpt.endswith("\n[truncated]")),
        omitted_keys=omitted_keys,
        reason="forced_projected" if representation == "projected" else "projected_shorter_than_raw",
        compact_structured_chars=len(compact_structured_text),
    )


def _projection_result(
    *,
    content: str | None,
    raw_chars: int,
    strategy: str,
    chosen_representation: str,
    content_state: str,
    truncated: bool,
    omitted_keys: list[str],
    reason: str,
    projected_candidate_chars: int | None = None,
    compact_structured_chars: int | None = None,
) -> dict[str, Any]:
    content_text = content or ""
    metadata: dict[str, Any] = {
        "strategy": strategy,
        "chosen_representation": chosen_representation,
        "reason": reason,
        "raw_chars": raw_chars,
        "projected_chars": len(content_text),
        "truncated": truncated,
        "omitted_keys": sorted(set(omitted_keys))[:24],
        "raw_content_state": "raw_readback_available",
    }
    if projected_candidate_chars is not None:
        metadata["projected_candidate_chars"] = projected_candidate_chars
    if compact_structured_chars is not None:
        metadata["compact_structured_chars"] = compact_structured_chars
    return {
        "content": content,
        "content_state": content_state,
        "truncated": truncated,
        "metadata": metadata,
    }


def _record_raw_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _auto_compact_structured_limit(max_chars: int) -> int:
    return max(400, min(max_chars, 1400))


def _compact_structured_record_text(value: Any, *, max_chars: int) -> tuple[str, list[str]]:
    omitted: list[str] = []
    compact_value = _compact_structured_record_value(value, omitted=omitted, path=(), depth=0, max_chars=max_chars)
    if compact_value in ({}, [], None):
        return "", omitted
    try:
        text = json.dumps(compact_value, ensure_ascii=False, default=str)
    except Exception:
        text = str(compact_value)
    return text, omitted


def _compact_structured_record_value(
    value: Any,
    *,
    omitted: list[str],
    path: tuple[str, ...],
    depth: int,
    max_chars: int,
) -> Any:
    if depth > 5:
        omitted.append(".".join(path) or "depth")
        return None
    if isinstance(value, Mapping):
        compact: dict[str, Any] = {}
        for index, (raw_key, child) in enumerate(value.items()):
            if index >= 48:
                omitted.append(".".join((*path, "field_limit")))
                break
            key = str(raw_key)
            normalized_key = key.strip().lower()
            child_path = (*path, key)
            if normalized_key in _PROJECTION_OMIT_KEYS:
                omitted.append(".".join(child_path))
                continue
            child_value = _compact_structured_record_value(
                child,
                omitted=omitted,
                path=child_path,
                depth=depth + 1,
                max_chars=max_chars,
            )
            if child_value not in ({}, [], None):
                compact[key] = child_value
        return compact
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        compact_items: list[Any] = []
        for index, child in enumerate(value):
            if index >= 24:
                omitted.append(".".join((*path, "item_limit")))
                compact_items.append({"omitted_items": len(value) - 24})
                break
            child_value = _compact_structured_record_value(
                child,
                omitted=omitted,
                path=(*path, str(index)),
                depth=depth + 1,
                max_chars=max_chars,
            )
            if child_value not in ({}, [], None):
                compact_items.append(child_value)
        return compact_items
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        text_limit = max(240, min(1200, max_chars // 2))
        return _excerpt(value, text_limit)
    return value


def _original_record_ref(ref: RecordRef) -> dict[str, Any]:
    return {
        "record_id": ref.get("id"),
        "collection": ref.get("collection"),
        "kind": ref.get("kind"),
        "path": ref.get("path"),
        "sha256": ref.get("sha256"),
        "size": ref.get("size"),
        "content_state": "raw_readback_available",
    }


def _record_projection_lines(value: Any) -> tuple[list[str], list[str]]:
    omitted: list[str] = []
    lines = _projection_lines(value, omitted=omitted, path=(), depth=0)
    compact: list[str] = []
    seen: set[str] = set()
    for line in lines:
        normalized = " ".join(str(line).split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        compact.append(normalized)
        if len(compact) >= 80:
            omitted.append("projection.line_limit")
            break
    return compact, omitted


def _projection_lines(value: Any, *, omitted: list[str], path: tuple[str, ...], depth: int) -> list[str]:
    if depth > 6:
        omitted.append(".".join(path) or "depth")
        return []
    if isinstance(value, Mapping):
        return _mapping_projection_lines(value, omitted=omitted, path=path, depth=depth)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return _sequence_projection_lines(value, omitted=omitted, path=path, depth=depth)
    line = _scalar_projection_line(path[-1] if path else "", value)
    return [line] if line else []


def _mapping_projection_lines(
    value: Mapping[str, Any],
    *,
    omitted: list[str],
    path: tuple[str, ...],
    depth: int,
) -> list[str]:
    lines: list[str] = []
    ordered_keys = sorted(value.keys(), key=_projection_key_order)
    for raw_key in ordered_keys:
        key = str(raw_key)
        normalized_key = key.strip().lower()
        child_path = (*path, key)
        child = value[raw_key]
        if normalized_key in _PROJECTION_OMIT_KEYS:
            omitted.append(".".join(child_path))
            continue
        if normalized_key == "rows" and isinstance(child, Sequence) and not isinstance(child, (str, bytes, bytearray)):
            lines.extend(_row_projection_lines(child, omitted=omitted, path=child_path, depth=depth + 1))
            continue
        if normalized_key == "conversation" and isinstance(child, Sequence) and not isinstance(child, (str, bytes, bytearray)):
            lines.extend(_conversation_projection_lines(child, omitted=omitted, path=child_path, depth=depth + 1))
            continue
        if normalized_key in {"raw_lines", "parts"}:
            lines.extend(_projection_lines(child, omitted=omitted, path=child_path, depth=depth + 1))
            continue
        if normalized_key in _PLAIN_TEXT_KEYS and isinstance(child, str):
            lines.extend(_plain_text_lines(child))
            continue
        lines.extend(_projection_lines(child, omitted=omitted, path=child_path, depth=depth + 1))
    return lines


def _sequence_projection_lines(
    value: Sequence[Any],
    *,
    omitted: list[str],
    path: tuple[str, ...],
    depth: int,
) -> list[str]:
    if _looks_like_field_value_rows(value):
        return _field_value_projection_lines(value, omitted=omitted, path=path, depth=depth + 1)
    if _looks_like_pair_rows(value):
        return _row_projection_lines(value, omitted=omitted, path=path, depth=depth + 1)
    lines: list[str] = []
    for index, item in enumerate(value):
        if index >= 80:
            omitted.append(".".join((*path, "items_after_80")))
            break
        item_path = (*path, str(index))
        if isinstance(item, Mapping) and "text" in item:
            speaker = item.get("speaker")
            text = str(item.get("text") or "").strip()
            if text:
                lines.append(f"{ speaker }: { text }" if speaker else text)
            continue
        lines.extend(_projection_lines(item, omitted=omitted, path=item_path, depth=depth + 1))
    return lines


def _field_value_projection_lines(
    rows: Sequence[Any],
    *,
    omitted: list[str],
    path: tuple[str, ...],
    depth: int,
) -> list[str]:
    lines: list[str] = []
    for index, item in enumerate(rows):
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("field") or item.get("key") or item.get("name") or "").strip()
        value = item.get("value")
        if key.lower() in _PROJECTION_OMIT_KEYS:
            omitted.append(".".join((*path, str(index), key)))
            continue
        if key.lower() in _PLAIN_TEXT_KEYS and isinstance(value, str):
            lines.extend(_plain_text_lines(value))
            continue
        line = _scalar_projection_line(key, value)
        if line:
            lines.append(line)
    return lines


def _row_projection_lines(
    rows: Sequence[Any],
    *,
    omitted: list[str],
    path: tuple[str, ...],
    depth: int,
) -> list[str]:
    lines: list[str] = []
    for index, row in enumerate(rows):
        if index >= 80:
            omitted.append(".".join((*path, "rows_after_80")))
            break
        if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)) and len(row) >= 2:
            key = str(row[0]).strip()
            value = row[1]
            if key.lower() in _PROJECTION_OMIT_KEYS:
                omitted.append(".".join((*path, str(index), key)))
                continue
            if key.lower() in _PLAIN_TEXT_KEYS and isinstance(value, str):
                lines.extend(_plain_text_lines(value))
                continue
            line = _scalar_projection_line(key, value)
            if line:
                lines.append(line)
            continue
        lines.extend(_projection_lines(row, omitted=omitted, path=(*path, str(index)), depth=depth + 1))
    return lines


def _conversation_projection_lines(
    rows: Sequence[Any],
    *,
    omitted: list[str],
    path: tuple[str, ...],
    depth: int,
) -> list[str]:
    lines: list[str] = []
    for index, row in enumerate(rows):
        if index >= 80:
            omitted.append(".".join((*path, "conversation_after_80")))
            break
        if isinstance(row, Mapping):
            speaker = row.get("speaker") or row.get("role")
            text = row.get("text") or row.get("content") or row.get("message")
            if text not in (None, "", [], {}):
                line = str(text).strip()
                lines.append(f"{ speaker }: { line }" if speaker else line)
            continue
        lines.extend(_projection_lines(row, omitted=omitted, path=(*path, str(index)), depth=depth + 1))
    return lines


def _looks_like_field_value_rows(value: Sequence[Any]) -> bool:
    return bool(value) and all(
        isinstance(item, Mapping) and ("field" in item or "key" in item or "name" in item) and "value" in item
        for item in value[:8]
    )


def _looks_like_pair_rows(value: Sequence[Any]) -> bool:
    return bool(value) and all(
        isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)) and len(item) >= 2
        for item in value[:8]
    )


def _projection_key_order(key: Any) -> tuple[int, str]:
    text = str(key).strip().lower()
    preferred = {
        "subject": 0,
        "title": 0,
        "headline": 0,
        "summary": 0,
        "body": 1,
        "content": 1,
        "text": 1,
        "message": 1,
        "raw_lines": 1,
        "conversation": 1,
        "parts": 1,
        "rows": 1,
    }
    return (preferred.get(text, 5), text)


def _scalar_projection_line(key: str, value: Any) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, (Mapping, Sequence)) and not isinstance(value, (str, bytes, bytearray)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    text = " ".join(text.split())
    if not text:
        return None
    key = str(key or "").strip()
    if not key or key.lower() in _PLAIN_TEXT_KEYS:
        return text
    return f"{ key }: { text }"


def _plain_text_lines(text: str) -> list[str]:
    return [" ".join(line.split()) for line in str(text).splitlines() if line.strip()]


def _char_budget(budget: Mapping[str, Any]) -> int:
    chars = budget.get("chars")
    if isinstance(chars, int):
        return max(1, chars)
    tokens = budget.get("tokens")
    if isinstance(tokens, int):
        return max(1, tokens * 4)
    return 12000


def _item_char_budget(budget: Mapping[str, Any], char_budget: int) -> int:
    item_chars = budget.get("item_chars")
    if isinstance(item_chars, int):
        return max(1, min(char_budget, item_chars))
    return max(1, min(char_budget, 2400))


def _excerpt(content: str | None, max_chars: int) -> str | None:
    if content is None:
        return None
    if max_chars <= 0:
        return ""
    if len(content) <= max_chars:
        return content
    return content[: max(0, max_chars - 15)].rstrip() + "\n[truncated]"
