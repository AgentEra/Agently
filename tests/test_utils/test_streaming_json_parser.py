import importlib

import pytest
from agently.utils.StreamingJSONParser import StreamingJSONParser


@pytest.mark.asyncio
async def test_streaming_json_parser_basic():
    schema = {
        "response": {
            "user": {
                "profile": {
                    "name": (str,),
                    "age": (int,),
                    "active": (bool,),
                },
                "preferences": {
                    "theme": (str,),
                    "notifications": (bool,),
                },
            },
            "data": {
                "items": [
                    {
                        "id": (int,),
                        "title": (str,),
                        "tags": [(str,)],
                    }
                ],
                "metadata": {
                    "total": (int,),
                    "page": (int,),
                },
            },
        }
    }

    def mock_llm_stream():
        chunks = [
            '{"response": {"user": {"profile": {"name": "J',
            'oh',
            'n", "age": 25, "active": true}, "preferences": {"theme": "dark", "notifications": true}}, "data": {"items": [{"id": 1, "title": "First',
            ' Item", "tags": ["tag1", "tag2"]}], "metadata": {"total": 1, "page": 1}}}}',
        ]
        for chunk in chunks:
            yield chunk

    parser = StreamingJSONParser(schema)
    events = []
    for chunk in mock_llm_stream():
        async for item in parser.parse_chunk(chunk):
            events.append(item)
    async for item in parser.finalize():
        events.append(item)

    # Extract delta and done events
    deltas = [e for e in events if e.event_type == "delta"]
    dones = [e for e in events if e.event_type == "done"]

    assert any("response.user.profile.name" in e.path and e.delta for e in deltas)
    assert any(e.path == "response.user.profile.name" and e.is_complete for e in dones)
    assert any(e.path == "response.data.items[0].tags" and e.value == ["tag1", "tag2"] for e in dones)
    assert events[-1].path == "response"  # Final completion of root


@pytest.mark.asyncio
async def test_streaming_json_parser_edge_cases():
    schema = {
        "profile": {
            "username": (str,),
            "age": (int,),
            "emails": [(str,)],
            "preferences": {
                "notifications": (bool,),
                "languages": [(str,)],
            },
            "optional_field": (str,),  # optional, may be missing
        }
    }

    # Simulate streaming chunks:
    # - username comes in three parts (testing delta aggregation)
    # - age completed in one chunk
    # - emails array built incrementally
    # - preferences.languages array partially missing one delta (simulate missing fragment)
    chunks = [
        '{"profile": {"username": "A',
        'l',
        'ice", "age": 30, "emails": ["alice@example.com"',
        ', "alice.work@example.com"], "preferences": {"notifications": true, "languages": ["English"',
        # missing next chunk for languages array (simulate missing delta)
        ', "Spanish"]}, "optional_field": "optional_value"}}',
    ]

    parser = StreamingJSONParser(schema)
    events = []
    for chunk in chunks:
        async for item in parser.parse_chunk(chunk):
            events.append(item)
    async for item in parser.finalize():
        events.append(item)

    # Collect done events by path
    done_events = [e for e in events if e.event_type == "done"]
    delta_events = [e for e in events if e.event_type == "delta"]

    # Check final values correctness
    # username final value
    username_done = next((e for e in done_events if e.path == "profile.username"), None)
    assert username_done is not None
    assert username_done.value == "Alice"

    # age final value
    age_done = next((e for e in done_events if e.path == "profile.age"), None)
    assert age_done is not None
    assert age_done.value == 30

    # emails final value
    emails_done = next((e for e in done_events if e.path == "profile.emails"), None)
    assert emails_done is not None
    assert emails_done.value == ["alice@example.com", "alice.work@example.com"]

    # preferences.notifications final value
    notifications_done = next((e for e in done_events if e.path == "profile.preferences.notifications"), None)
    assert notifications_done is not None
    assert notifications_done.value is True

    # preferences.languages final value
    languages_done = next((e for e in done_events if e.path == "profile.preferences.languages"), None)
    assert languages_done is not None
    # Because one delta chunk was missing, the final value should still be complete and correct
    assert languages_done.value == ["English", "Spanish"]

    # optional_field final value
    optional_done = next((e for e in done_events if e.path == "profile.optional_field"), None)
    assert optional_done is not None
    assert optional_done.value == "optional_value"

    # Check that username deltas appear as expected (3 parts)
    username_deltas = [e for e in delta_events if e.path == "profile.username"]
    assert len(username_deltas) >= 3
    aggregated_username = "".join(e.delta for e in username_deltas)
    assert aggregated_username == "Alice"

    # age done should appear after age delta(s) if any delta exist
    age_deltas = [e for e in delta_events if e.path == "profile.age"]
    if age_deltas:
        assert events.index(age_deltas[-1]) < events.index(age_done)
    # else: no delta for age is expected and acceptable

    # emails done after all email items deltas
    emails_deltas = [e for e in delta_events if e.path.startswith("profile.emails")]
    assert emails_deltas
    assert events.index(emails_deltas[-1]) < events.index(emails_done)

    # languages done after all language items deltas (including missing delta simulated)
    languages_deltas = [e for e in delta_events if e.path.startswith("profile.preferences.languages")]
    assert languages_deltas
    assert events.index(languages_deltas[-1]) < events.index(languages_done)


@pytest.mark.asyncio
async def test_streaming_json_parser_root_list_schema():
    schema = [
        {
            "title": (str,),
            "url": (str,),
            "tags": [(str,)],
        }
    ]

    chunks = [
        '[{"title": "B',
        'reaking News", "url": "https://example.com/news", "tags": ["world", "top"]}]',
    ]

    parser = StreamingJSONParser(schema)
    events = []
    for chunk in chunks:
        async for item in parser.parse_chunk(chunk):
            events.append(item)
    async for item in parser.finalize():
        events.append(item)

    done_events = [e for e in events if e.event_type == "done"]
    delta_events = [e for e in events if e.event_type == "delta"]

    title_done = next((e for e in done_events if e.path == "[0].title"), None)
    tags_done = next((e for e in done_events if e.path == "[0].tags"), None)

    assert title_done is not None
    assert title_done.value == "Breaking News"
    assert tags_done is not None
    assert tags_done.value == ["world", "top"]
    assert any(e.path == "[0].title" and e.delta for e in delta_events)


@pytest.mark.asyncio
async def test_streaming_json_parser_distinguishes_observed_and_synthetic_completion():
    parser = StreamingJSONParser({"items": [{"id": (int,)}]})
    events = []

    for chunk in ['{"items":[{"id":1},', '{"id":2']:
        async for item in parser.parse_chunk(chunk):
            events.append(item)
    async for item in parser.finalize():
        events.append(item)

    first_item_done = next(
        item
        for item in events
        if item.path == "items[0]" and item.event_type == "done"
    )
    second_item_done = next(
        item
        for item in events
        if item.path == "items[1]" and item.event_type == "done"
    )

    assert first_item_done.completion_source == "observed_boundary"
    assert second_item_done.completion_source == "synthetic_repair"


@pytest.mark.asyncio
async def test_streaming_json_parser_marks_trusted_flush_as_final_reconciliation():
    parser = StreamingJSONParser({"items": [{"id": (int,)}]})
    events = []

    async for item in parser.parse_chunk('{"items":[{"id":1'):
        events.append(item)
    async for item in parser.flush_final_data(
        {"items": [{"id": 1}]},
        completion_source="final_reconciliation",
    ):
        events.append(item)

    item_done = next(
        item
        for item in events
        if item.path == "items[0]" and item.event_type == "done"
    )
    assert item_done.completion_source == "final_reconciliation"


@pytest.mark.asyncio
async def test_streaming_json_parser_skips_large_incomplete_parse(monkeypatch):
    schema = {"report": (str,)}
    parser = StreamingJSONParser(schema, max_incomplete_parse_chars=32)

    def fail_if_called(_value):
        raise AssertionError("large incomplete JSON should not be parsed incrementally")

    parser_module = importlib.import_module("agently.utils.StreamingJSONParser")
    monkeypatch.setattr(parser_module.json5, "loads", fail_if_called)

    events = []
    async for item in parser.parse_chunk('{"report": "' + ("x" * 128)):
        events.append(item)

    assert len(events) == 1
    assert events[0].path == "$status"
    assert events[0].value["status"] == "streaming_parse_deferred"
    assert events[0].value["reason"] == "large_json_incremental_parse"


@pytest.mark.asyncio
async def test_streaming_json_parser_treats_none_threshold_as_default(monkeypatch):
    schema = {"report": (str,)}
    parser = StreamingJSONParser(schema, max_incomplete_parse_chars=None)

    def fail_if_called(_value):
        raise AssertionError("None threshold should keep the default large-delta guard")

    parser_module = importlib.import_module("agently.utils.StreamingJSONParser")
    monkeypatch.setattr(parser_module.json5, "loads", fail_if_called)

    events = []
    async for item in parser.parse_chunk('{"report": "' + ("x" * 20000)):
        events.append(item)

    assert len(events) == 1
    assert events[0].path == "$status"
    assert events[0].value["status"] == "streaming_parse_deferred"


@pytest.mark.asyncio
async def test_streaming_json_parser_emits_large_deferred_status_once(monkeypatch):
    schema = {"report": (str,)}
    parser = StreamingJSONParser(schema, max_incomplete_parse_chars=32)

    def fail_if_called(_value):
        raise AssertionError("large JSON deltas should stay deferred until finalize")

    parser_module = importlib.import_module("agently.utils.StreamingJSONParser")
    monkeypatch.setattr(parser_module.json5, "loads", fail_if_called)

    events = []
    for chunk in ['{"report": "' + ("x" * 128), "more text", "even more text"]:
        async for item in parser.parse_chunk(chunk):
            events.append(item)

    assert [item.path for item in events] == ["$status"]
    assert events[0].value["status"] == "streaming_parse_deferred"


@pytest.mark.asyncio
async def test_streaming_json_parser_flushes_observed_prefix_after_large_defer():
    parser = StreamingJSONParser(
        {"items": [{"value": (str,)}]},
        max_incomplete_parse_chars=32,
    )
    events = []
    raw = (
        '{"items":[{"value":"'
        + ("a" * 128)
        + '"},{"value":"open'
    )

    async for item in parser.parse_chunk(raw):
        events.append(item)
    async for item in parser.flush_observed_boundaries():
        events.append(item)

    first_item = next(
        item
        for item in events
        if item.path == "items[0]" and item.event_type == "done"
    )
    assert first_item.value == {"value": "a" * 128}
    assert first_item.completion_source == "observed_boundary"
    assert not [
        item
        for item in events
        if item.path == "items[1]" and item.is_complete
    ]


@pytest.mark.asyncio
async def test_streaming_json_parser_parses_large_complete_payload_after_threshold():
    schema = {"report": (str,)}
    parser = StreamingJSONParser(schema, max_incomplete_parse_chars=32)

    events = []
    async for item in parser.parse_chunk('{"report": "' + ("x" * 128) + '"}'):
        events.append(item)
    async for item in parser.finalize():
        events.append(item)

    assert any(item.path == "$status" for item in events)
    assert any(item.path == "report" and item.event_type == "done" for item in events)
