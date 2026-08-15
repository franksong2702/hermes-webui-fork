"""Regression coverage for #6920 cancel-time run-journal fallback."""

from __future__ import annotations

import json
import queue
import threading
from unittest.mock import Mock, patch

import pytest

import api.config as config
import api.models as models
from api.models import Session
from api.run_journal import RunJournalWriter
from api.streaming import _context_messages_for_new_turn, cancel_stream


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    for mapping_name in (
        "STREAMS",
        "CANCEL_FLAGS",
        "AGENT_INSTANCES",
        "STREAM_PARTIAL_TEXT",
        "STREAM_REASONING_TEXT",
        "STREAM_LIVE_TOOL_CALLS",
    ):
        getattr(config, mapping_name).clear()
    config.ACTIVE_RUNS.clear()
    config.SESSION_AGENT_LOCKS.clear()
    yield
    models.SESSIONS.clear()
    for mapping_name in (
        "STREAMS",
        "CANCEL_FLAGS",
        "AGENT_INSTANCES",
        "STREAM_PARTIAL_TEXT",
        "STREAM_REASONING_TEXT",
        "STREAM_LIVE_TOOL_CALLS",
    ):
        getattr(config, mapping_name).clear()
    config.ACTIVE_RUNS.clear()
    config.SESSION_AGENT_LOCKS.clear()


def _session(session_id: str, stream_id: str) -> Session:
    session = Session(
        session_id=session_id,
        title="Issue 6920",
        messages=[],
        context_messages=[],
        pending_user_message="Continue this cancelled turn",
        pending_started_at=1.0,
        active_stream_id=stream_id,
    )
    session.save()
    models.SESSIONS[session_id] = session
    return session


def _start_cancel_state(session_id: str, stream_id: str):
    config.STREAMS[stream_id] = queue.Queue()
    config.CANCEL_FLAGS[stream_id] = threading.Event()
    agent = Mock()
    agent.session_id = session_id
    agent.interrupt = Mock()
    config.AGENT_INSTANCES[stream_id] = agent
    return agent


def _assistant_rows(session):
    return [
        message for message in session.messages
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]


def test_empty_live_buffers_recover_journal_prose_before_cancel_marker():
    sid = "issue6920_prose"
    stream_id = "stream-prose"
    _session(sid, stream_id)
    _start_cancel_state(sid, stream_id)
    RunJournalWriter(sid, stream_id).append_sse_event(
        "token", {"text": "Visible work survived in the journal."},
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    assistant = _assistant_rows(reloaded)
    partial = [row for row in assistant if row.get("_partial")]
    assert [row.get("content") for row in partial] == [
        "Visible work survived in the journal.",
    ]
    marker_index = next(index for index, row in enumerate(assistant) if row.get("_error"))
    partial_index = next(
        index for index, row in enumerate(assistant) if row.get("_partial")
    )
    assert partial_index < marker_index
    assert marker_index == len(assistant) - 1

    context = _context_messages_for_new_turn(reloaded, "What happened?")
    assert any(
        row.get("content") == "Visible work survived in the journal."
        for row in context
    )


def test_empty_live_buffers_recover_reasoning_only_without_context_leak():
    sid = "issue6920_reasoning"
    stream_id = "stream-reasoning"
    _session(sid, stream_id)
    _start_cancel_state(sid, stream_id)
    RunJournalWriter(sid, stream_id).append_sse_event(
        "reasoning", {"text": "Display-only reasoning from the cancelled turn."},
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    partial = [row for row in _assistant_rows(reloaded) if row.get("_partial")]
    assert len(partial) == 1
    assert partial[0].get("content") == ""
    assert partial[0].get("reasoning") == (
        "Display-only reasoning from the cancelled turn."
    )
    assert "Display-only reasoning" not in json.dumps(
        reloaded.context_messages, ensure_ascii=False,
    )


def test_empty_live_buffers_recover_tool_round_boundary_and_seal_completion():
    sid = "issue6920_tool"
    stream_id = "stream-tool"
    _session(sid, stream_id)
    _start_cancel_state(sid, stream_id)
    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event(
        "tool",
        {
            "name": "terminal",
            "preview": "printf recovered",
            "args": {"command": "printf recovered"},
        },
    )
    writer.append_sse_event(
        "tool_complete",
        {"name": "terminal", "duration": 0.25, "is_error": False},
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    partial = [row for row in _assistant_rows(reloaded) if row.get("_partial")]
    assert len(partial) == 1
    assert partial[0].get("content") == ""
    assert len(reloaded.tool_calls) == 1
    assert reloaded.tool_calls[0]["name"] == "terminal"
    assert reloaded.tool_calls[0]["done"] is True
    assert reloaded.tool_calls[0]["preview"] == "printf recovered"
    assert reloaded.tool_calls[0]["_recovered_stream_id"] == stream_id


def test_multi_round_journal_fallback_keeps_prose_tool_data_and_order():
    sid = "issue6920_multi_round"
    stream_id = "stream-multi-round"
    _session(sid, stream_id)
    _start_cancel_state(sid, stream_id)
    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event("token", {"text": "First visible work."})
    writer.append_sse_event(
        "tool",
        {
            "name": "terminal",
            "preview": "printf first",
            "args": {"command": "printf first"},
        },
    )
    writer.append_sse_event(
        "tool_complete",
        {"name": "terminal", "preview": "first result", "duration": 0.5},
    )
    writer.append_sse_event(
        "interim_assistant", {"text": "A second visible work row."},
    )
    writer.append_sse_event("token", {"text": "Final visible work."})

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    assistant = _assistant_rows(reloaded)
    partial = [row for row in assistant if row.get("_partial")]
    assert [row.get("content") for row in partial] == [
        "First visible work.",
        "A second visible work row.",
        "Final visible work.",
    ]
    assert len(reloaded.tool_calls) == 1
    assert reloaded.tool_calls[0]["name"] == "terminal"
    assert reloaded.tool_calls[0]["preview"] == "first result"
    assert reloaded.tool_calls[0]["args"] == {"command": "printf first"}
    assert reloaded.tool_calls[0]["done"] is True
    assert reloaded.tool_calls[0]["_partial"] is True
    marker_index = next(index for index, row in enumerate(assistant) if row.get("_error"))
    assert marker_index == len(assistant) - 1


def test_live_partial_buffer_wins_without_journal_duplicate():
    sid = "issue6920_live_priority"
    stream_id = "stream-live-priority"
    session = _session(sid, stream_id)
    _start_cancel_state(sid, stream_id)
    config.STREAM_PARTIAL_TEXT[stream_id] = "Live buffer is authoritative."
    RunJournalWriter(sid, stream_id).append_sse_event(
        "token", {"text": "Journal copy must not be appended."},
    )

    with patch("api.models._append_journaled_partial_output") as fallback:
        assert cancel_stream(stream_id) is True

    fallback.assert_not_called()
    partial = [row for row in _assistant_rows(session) if row.get("_partial")]
    assert [row.get("content") for row in partial] == [
        "Live buffer is authoritative.",
    ]


def test_foreign_and_malformed_journal_fail_soft_to_cancel_marker():
    sid = "issue6920_fail_soft"
    stream_id = "stream-current"
    session = _session(sid, stream_id)
    _start_cancel_state(sid, stream_id)
    with patch(
        "api.run_journal.read_run_events",
        return_value={
            "events": [
                {
                    "event": "token",
                    "type": "token",
                    "seq": 1,
                    "session_id": "foreign-session",
                    "run_id": "foreign-stream",
                    "event_id": "foreign-stream:1",
                    "payload": {"text": "Foreign run must be ignored."},
                },
                {"event": "token", "payload": "not-a-dict"},
            ],
        },
    ):
        assert cancel_stream(stream_id) is True

    assert not [row for row in _assistant_rows(session) if row.get("_partial")]
    assert len([row for row in _assistant_rows(session) if row.get("_error")]) == 1


def test_repeated_journal_fallback_dedupes_rows_and_does_not_mark_history_partial():
    sid = "issue6920_dedupe"
    stream_id = "stream-dedupe"
    session = _session(sid, stream_id)
    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event("token", {"text": "One durable partial."})
    _start_cancel_state(sid, stream_id)

    assert cancel_stream(stream_id) is True
    first_partial_count = len(
        [row for row in _assistant_rows(session) if row.get("_partial")]
    )
    assert first_partial_count == 1

    # The helper is intentionally idempotent when the same journal is seen
    # again; its default call must not retroactively mark existing history.
    from api.models import _append_journaled_partial_output

    assert _append_journaled_partial_output(
        session, stream_id, dedupe_existing=True, mark_partial=True,
    ) is False
    partial_rows = [row for row in _assistant_rows(session) if row.get("_partial")]
    assert len(partial_rows) == first_partial_count
    assert partial_rows[0]["content"] == "One durable partial."


def test_cancel_journal_dedupe_does_not_claim_same_text_from_an_older_turn():
    """Current cancelled work must stay after its owning user turn."""
    sid = "issue6920_turn_scoped_dedupe"
    stream_id = "stream-turn-scoped-dedupe"
    session = _session(sid, stream_id)
    session.messages = [
        {
            "role": "user",
            "content": "Earlier question",
            "timestamp": 1,
        },
        {
            "role": "assistant",
            "content": "Repeated answer",
            "timestamp": 2,
        },
    ]
    session.context_messages = [dict(message) for message in session.messages]
    session.pending_started_at = 10
    session.save()
    _start_cancel_state(sid, stream_id)
    RunJournalWriter(sid, stream_id).append_sse_event(
        "token", {"text": "Repeated answer"},
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    assert reloaded.messages[1] == {
        "role": "assistant",
        "content": "Repeated answer",
        "timestamp": 2,
    }
    current_user_index = next(
        index
        for index, message in enumerate(reloaded.messages)
        if message.get("role") == "user"
        and message.get("content") == "Continue this cancelled turn"
    )
    current_partial_index = next(
        index
        for index, message in enumerate(reloaded.messages)
        if message.get("role") == "assistant"
        and message.get("content") == "Repeated answer"
        and message.get("_partial") is True
    )
    marker_index = next(
        index
        for index, message in enumerate(reloaded.messages)
        if message.get("role") == "assistant" and message.get("_error") is True
    )
    assert current_user_index < current_partial_index < marker_index
    assert marker_index == len(reloaded.messages) - 1


def test_cancel_journal_context_includes_owning_user():
    """Recovered assistant progress keeps its cancelled user turn in context."""
    sid = "issue6920_context_owner"
    stream_id = "stream-context-owner"
    session = _session(sid, stream_id)
    session.messages = [
        {
            "role": "user",
            "content": "Earlier question",
            "timestamp": 1,
        },
        {
            "role": "assistant",
            "content": "Earlier answer",
            "timestamp": 2,
        },
    ]
    session.context_messages = [dict(message) for message in session.messages]
    session.pending_started_at = 10
    session.save()
    _start_cancel_state(sid, stream_id)
    writer = RunJournalWriter(sid, stream_id)
    writer.append_sse_event(
        "token", {"text": "Recovered assistant progress."},
    )
    writer.append_sse_event(
        "reasoning", {"text": "Display-only reasoning must stay hidden."},
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    context = reloaded.context_messages
    current_user_indexes = [
        index
        for index, message in enumerate(context)
        if message.get("role") == "user"
        and message.get("content") == "Continue this cancelled turn"
    ]
    recovered_indexes = [
        index
        for index, message in enumerate(context)
        if message.get("role") == "assistant"
        and message.get("content") == "Recovered assistant progress."
    ]
    assert len(current_user_indexes) == 1
    assert len(recovered_indexes) == 1
    assert recovered_indexes[0] == current_user_indexes[0] + 1
    assert "Display-only reasoning" not in json.dumps(
        context, ensure_ascii=False,
    )
    assert reloaded.pending_user_message is None
    assert reloaded.pending_attachments == []
    assert reloaded.pending_started_at is None
