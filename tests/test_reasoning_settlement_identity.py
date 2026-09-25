"""Durable reasoning identity at server hydration; regressions from PR #6304."""

import copy
import json
import pytest


def test_anchor_scene_preserves_identical_reasoning_with_distinct_durable_ids():
    from api import routes

    identityless_prefix = "legacy reasoning " + ("x" * 90)
    identityless_extension = f"{identityless_prefix} with more detail"
    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "Final answer"},
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "Final answer",
                "activity_rows": [
                    {
                        "row_id": "reasoning-row-a",
                        "event_id": "reasoning-event-a",
                        "local_id": "reasoning-local-a",
                        "role": "thinking",
                        "kind": "reasoning",
                        "source_event_type": "reasoning",
                        "status": "completed",
                        "text": "same reasoning",
                        "identity": {
                            "event_id": "reasoning-event-a",
                            "row_id": "reasoning-row-a",
                            "local_id": "reasoning-local-a",
                        },
                    },
                    {
                        "row_id": "hydrated:stream-1:prose:1:0",
                        "event_id": None,
                        "local_id": None,
                        "role": "prose",
                        "kind": "process_prose",
                        "source_event_type": "token",
                        "status": "completed",
                        "text": "same reasoning",
                        "identity": {
                            "event_id": None,
                            "row_id": None,
                            "local_id": None,
                        },
                    },
                    {
                        "row_id": "reasoning-row-b",
                        "event_id": "reasoning-event-b",
                        "local_id": "reasoning-local-b",
                        "role": "thinking",
                        "kind": "reasoning",
                        "source_event_type": "reasoning",
                        "status": "completed",
                        "text": "same reasoning",
                        "identity": {
                            "event_id": "reasoning-event-b",
                            "row_id": "reasoning-row-b",
                            "local_id": "reasoning-local-b",
                        },
                    },
                    {
                        "row_id": "reasoning-row-a-redelivery",
                        "event_id": "reasoning-event-a",
                        "local_id": "reasoning-local-a-redelivery",
                        "role": "thinking",
                        "kind": "reasoning",
                        "source_event_type": "reasoning",
                        "status": "completed",
                        "text": "same reasoning",
                        "identity": {"event_id": "reasoning-event-a"},
                    },
                    {
                        "row_id": "hydrated:stream-1:thinking:1:0",
                        "event_id": None,
                        "local_id": None,
                        "role": "thinking",
                        "kind": "reasoning",
                        "source_event_type": "reasoning",
                        "status": "completed",
                        "text": identityless_prefix,
                        "identity": {
                            "event_id": None,
                            "row_id": None,
                            "local_id": None,
                        },
                    },
                    {
                        "row_id": "hydrated:stream-1:thinking:1:1",
                        "event_id": None,
                        "local_id": None,
                        "role": "thinking",
                        "kind": "reasoning",
                        "source_event_type": "reasoning",
                        "status": "completed",
                        "text": identityless_extension,
                        "identity": {
                            "event_id": None,
                            "row_id": None,
                            "local_id": None,
                        },
                    },
                    {
                        "row_id": "hydrated:stream-1:thinking:1:2",
                        "event_id": None,
                        "local_id": None,
                        "role": "thinking",
                        "kind": "reasoning",
                        "source_event_type": "reasoning",
                        "status": "completed",
                        "text": identityless_prefix,
                        "identity": {
                            "event_id": None,
                            "row_id": None,
                            "local_id": None,
                        },
                    },
                    {
                        "row_id": "done",
                        "role": "terminal",
                        "kind": "terminal_status",
                        "source_event_type": "done",
                    },
                ],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records)
    thinking_rows = [
        row
        for row in hydrated[1]["_anchor_activity_scene"]["activity_rows"]
        if row.get("role") == "thinking"
    ]
    textual_rows = [
        row
        for row in hydrated[1]["_anchor_activity_scene"]["activity_rows"]
        if row.get("role") in ("prose", "thinking")
    ]

    assert [row.get("text") for row in thinking_rows] == [
        "same reasoning",
        "same reasoning",
        identityless_prefix,
        identityless_extension,
    ]
    assert [row.get("event_id") for row in thinking_rows] == [
        "reasoning-event-a",
        "reasoning-event-b",
        None,
        None,
    ]
    assert [row.get("row_id") for row in thinking_rows[:2]] == [
        "reasoning-row-a",
        "reasoning-row-b",
    ]
    assert [(row.get("role"), row.get("text")) for row in textual_rows] == [
        ("thinking", "same reasoning"),
        ("prose", "same reasoning"),
        ("thinking", "same reasoning"),
        ("thinking", identityless_prefix),
        ("thinking", identityless_extension),
    ]


def _row(
    text,
    identity=None,
    *,
    field="event_id",
    location="row",
    role="thinking",
    status="completed",
):
    row = {
        "role": role,
        "kind": "reasoning" if role == "thinking" else "process_prose",
        "source_event_type": "reasoning" if role == "thinking" else "token",
        "text": text,
        "status": status,
        "stream_id": "stream-1",
    }
    if identity is not None:
        target = row if location == "row" else row.setdefault(location, {})
        target[field] = identity
    else:
        row["row_id"] = "hydrated:stream-1:thinking:1:0"
    return row


def _settle(rows, *, reasoning=None):
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "Final answer"},
    ]
    if reasoning is not None:
        messages[-1]["reasoning_content"] = reasoning
    scene = {
        "version": "activity_scene_v1",
        "mode": "compact_worklog",
        "identity": {
            "session_id": "sid-1",
            "stream_id": "stream-1",
            "run_id": "stream-1",
        },
        "activity_rows": rows,
        "final_answer": "Final answer",
    }
    original = copy.deepcopy((messages, scene))
    record = {
        "message_index": 1,
        "stream_id": "stream-1",
        "scene": scene,
        "message_ref": routes._assistant_anchor_scene_message_ref(messages[-1]),
    }
    result = routes._hydrate_anchor_activity_scenes(
        messages, {record["message_ref"]: record}
    )[1]["_anchor_activity_scene"]
    assert (messages, scene) == original, (
        "projection must not mutate the input transcript or scene"
    )
    return result


@pytest.mark.parametrize("field", ["event_id", "row_id", "local_id"])
@pytest.mark.parametrize("location", ["row", "identity", "payload"])
def test_distinct_ids_survive_equal_text_and_exact_redelivery(field, location):
    a = _row("Repeated reasoning", "segment-a", field=field, location=location)
    b = _row("Repeated reasoning", "segment-b", field=field, location=location)
    result = _settle([a, b, copy.deepcopy(a)])
    thoughts = [r for r in result["activity_rows"] if r["role"] == "thinking"]
    assert len(thoughts) == 2
    identities = [r if location == "row" else r[location] for r in thoughts]
    assert [r[field] for r in identities] == ["segment-a", "segment-b"]


@pytest.mark.parametrize("redeliver", [False, True])
def test_transcript_aggregate_keeps_live_segment_identities(redeliver):
    a = _row("Plan. ", "event-a", status="running")
    b = _row("Verify.", "event-b", status="running")
    result = _settle(
        [a, b] + ([copy.deepcopy(a)] if redeliver else []), reasoning="Plan. Verify."
    )
    thoughts = [r for r in result["activity_rows"] if r["role"] == "thinking"]
    assert [r.get("event_id") for r in thoughts] == ["event-a", "event-b"]
    assert [r["status"] for r in thoughts] == ["completed", "completed"]


@pytest.mark.parametrize("identity_first", [True, False])
def test_legacy_projection_dedupes_without_erasing_other_roles(identity_first):
    plain = _row("Same words")
    identified = _row("Same words", "event-a")
    rows = [identified, plain] if identity_first else [plain, identified]
    rows += [_row("Same words", role="prose"), _row("Same words", "event-b")]
    result = _settle(rows)
    assert [(r["role"], r.get("event_id")) for r in result["activity_rows"]] == [
        ("thinking", "event-a"),
        ("prose", None),
        ("thinking", "event-b"),
    ]


def test_repeated_settlement_and_json_reload_keep_distinct_ids():
    first = _settle([_row("Same", "event-a"), _row("Same", "event-b")])
    second = _settle(json.loads(json.dumps(first))["activity_rows"])
    assert [(r["text"], r.get("event_id")) for r in second["activity_rows"]] == [
        ("Same", "event-a"),
        ("Same", "event-b"),
    ]


@pytest.mark.parametrize("invalid_id", [{}, [], True])
def test_malformed_identity_does_not_merge_distinct_text(invalid_id):
    result = _settle(
        [_row("First observation", invalid_id), _row("Second observation", invalid_id)]
    )
    assert [r["text"] for r in result["activity_rows"]] == [
        "First observation",
        "Second observation",
    ]
