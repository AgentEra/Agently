"""RecordStore rerank provider seam: scoring ownership, ordering and degrade paths."""

import tempfile

import pytest

from agently.base import record_store_registry
from agently.core.storage import AgentRerankProvider, CallableRerankProvider
from agently.core.storage.Errors import RecordStoreConfigurationError
from agently.types.plugins import RerankProvider


NOTES = ["alpha note", "beta note", "gamma note"]


async def _seeded_store(**options):
    store = record_store_registry.create(tempfile.mkdtemp(), mode="read_write", **options)
    for note in NOTES:
        await store.put({"text": note}, collection="notes", summary=note, indexed=True)
    return store


def _scores_by_text(mapping):
    """Score by summary text so tests do not depend on candidate ordering."""

    def rerank(query, documents, top_n=None):
        results = []
        for index, document in enumerate(documents):
            for text, score in mapping.items():
                if document.startswith(text):
                    results.append({"index": index, "relevance_score": score})
                    break
        return results

    return rerank


def test_builtin_providers_satisfy_the_protocol():
    assert isinstance(CallableRerankProvider(lambda q, d, top_n=None: []), RerankProvider)
    assert isinstance(AgentRerankProvider(object()), RerankProvider)


def test_registry_lists_builtin_rerank_providers():
    assert {"callable", "agent"}.issubset(set(record_store_registry.list_rerank_providers()))


def test_unregistered_rerank_provider_fails_closed():
    # Component options are validated when the backend materializes, matching embedding.
    store = record_store_registry.create(tempfile.mkdtemp(), mode="read_write", rerank_provider="nope")
    with pytest.raises(RecordStoreConfigurationError, match="rerank provider is not registered"):
        store.backend


def test_rerank_provider_cannot_combine_with_full_backend_replacement():
    store = record_store_registry.create(
        tempfile.mkdtemp(),
        mode="read_write",
        provider="sqlite",
        rerank_provider="callable",
        rerank_options={"rerank_function": lambda q, d, top_n=None: []},
    )
    with pytest.raises(RecordStoreConfigurationError, match="cannot be combined"):
        store.backend


def test_callable_provider_requires_a_callable_option():
    with pytest.raises(RecordStoreConfigurationError, match="rerank_function"):
        record_store_registry._create_callable_rerank_provider()


def test_agent_provider_requires_an_agent_option():
    with pytest.raises(RecordStoreConfigurationError, match="rerank_agent"):
        record_store_registry._create_agent_rerank_provider()


@pytest.mark.asyncio
async def test_provider_orders_candidates_by_relevance_score():
    store = await _seeded_store(
        rerank_provider="callable",
        rerank_options={
            "rerank_function": _scores_by_text({"alpha": 0.1, "beta": 0.9, "gamma": 0.5}),
        },
    )

    package = await store.retrieve("note", rerank=True)

    assert package["diagnostics"]["rerank"]["engine"] == "provider"
    assert package["diagnostics"]["rerank"]["dropped"] == 0
    assert [item.get("summary") for item in package["items"]] == ["beta note", "gamma note", "alpha note"]


@pytest.mark.asyncio
async def test_provider_keeps_unscored_candidates_behind_scored_ones():
    store = await _seeded_store(
        rerank_provider="callable",
        rerank_options={"rerank_function": _scores_by_text({"gamma": 0.7})},
    )

    package = await store.retrieve("note", rerank=True)

    summaries = [item.get("summary") for item in package["items"]]
    assert summaries[0] == "gamma note"
    assert set(summaries[1:]) == {"alpha note", "beta note"}
    assert package["diagnostics"]["rerank"]["unscored"] == 2
    assert package["diagnostics"]["rerank"]["dropped"] == 0


@pytest.mark.asyncio
async def test_provider_drops_only_under_an_explicit_score_floor():
    store = await _seeded_store(
        rerank_provider="callable",
        rerank_options={
            "rerank_function": _scores_by_text({"alpha": 0.01, "beta": 0.9, "gamma": 0.8}),
        },
    )

    package = await store.retrieve("note", rerank=True, budget={"rerank_score_floor": 0.5})

    assert [item.get("summary") for item in package["items"]] == ["beta note", "gamma note"]
    assert package["diagnostics"]["rerank"]["dropped"] == 1
    assert package["diagnostics"]["rerank"]["score_floor"] == 0.5
    assert {"reason": "rerank_drop", "count": 1} in package["omitted"]


@pytest.mark.asyncio
async def test_provider_ignores_out_of_range_and_duplicate_indexes():
    def rerank(query, documents, top_n=None):
        return [
            {"index": 0, "relevance_score": 0.4},
            {"index": 0, "relevance_score": 0.99},
            {"index": 99, "relevance_score": 1.0},
            {"index": -1, "relevance_score": 1.0},
        ]

    store = await _seeded_store(
        rerank_provider="callable", rerank_options={"rerank_function": rerank}
    )

    package = await store.retrieve("note", rerank=True)

    assert package["diagnostics"]["rerank"]["scored"] == 1
    assert package["diagnostics"]["rerank"]["unscored"] == 2
    assert len(package["items"]) == 3


@pytest.mark.asyncio
async def test_provider_failure_degrades_without_falling_back_to_model_rerank():
    def rerank(query, documents, top_n=None):
        raise RuntimeError("reranker offline")

    store = await _seeded_store(
        rerank_provider="callable", rerank_options={"rerank_function": rerank}
    )

    package = await store.retrieve("note", rerank=True)

    diagnostics = package["diagnostics"]["rerank"]
    assert diagnostics["engine"] == "provider"
    assert diagnostics["degraded"] is True
    assert diagnostics["reason"] == "rerank_failed"
    assert "reranker offline" in str(diagnostics["error"])
    assert len(package["items"]) == 3


@pytest.mark.asyncio
async def test_empty_provider_reply_degrades_instead_of_emptying_results():
    store = await _seeded_store(
        rerank_provider="callable", rerank_options={"rerank_function": lambda q, d, top_n=None: []}
    )

    package = await store.retrieve("note", rerank=True)

    assert package["diagnostics"]["rerank"]["degraded"] is True
    assert len(package["items"]) == 3


@pytest.mark.asyncio
async def test_explicit_handler_still_wins_over_a_bound_provider():
    provider_calls: list[str] = []

    def rerank(query, documents, top_n=None):
        provider_calls.append(query)
        return [{"index": 0, "relevance_score": 1.0}]

    store = await _seeded_store(
        rerank_provider="callable", rerank_options={"rerank_function": rerank}
    )

    def handler(query, candidates):
        return {"decisions": [{"id": candidate["id"], "useful": True, "score": 0.5} for candidate in candidates]}

    package = await store.retrieve("note", rerank=True, rerank_handler=handler)

    assert package["diagnostics"]["rerank"]["engine"] == "handler"
    assert provider_calls == []


@pytest.mark.asyncio
async def test_rerank_disabled_never_reaches_the_provider():
    provider_calls: list[str] = []

    def rerank(query, documents, top_n=None):
        provider_calls.append(query)
        return [{"index": 0, "relevance_score": 1.0}]

    store = await _seeded_store(
        rerank_provider="callable", rerank_options={"rerank_function": rerank}
    )

    package = await store.retrieve("note", rerank=False)

    assert package["diagnostics"]["rerank"] == {"enabled": False, "reason": "disabled"}
    assert provider_calls == []


@pytest.mark.asyncio
async def test_unbound_store_keeps_the_model_rerank_engine():
    store = await _seeded_store()

    package = await store.retrieve("note", rerank=True, rerank_handler=lambda query, candidates: {"decisions": []})

    assert package["diagnostics"]["rerank"]["engine"] == "handler"


@pytest.mark.asyncio
async def test_async_callable_provider_is_awaited():
    async def rerank(query, documents, top_n=None):
        return [{"index": index, "relevance_score": float(index)} for index in range(len(documents))]

    store = await _seeded_store(
        rerank_provider="callable", rerank_options={"rerank_function": rerank}
    )

    package = await store.retrieve("note", rerank=True)

    assert package["diagnostics"]["rerank"]["engine"] == "provider"
    scores = [float(item.get("score") or 0.0) for item in package["items"]]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_provider_object_can_be_bound_directly():
    class StaticReranker:
        name = "static"

        async def rank(self, query, documents, *, top_n=None):
            return [{"index": len(documents) - 1, "score": 1.0}]

    store = await _seeded_store(rerank_provider=StaticReranker())

    package = await store.retrieve("note", rerank=True)

    assert package["diagnostics"]["rerank"]["provider"] == "static"
    assert package["diagnostics"]["rerank"]["scored"] == 1
