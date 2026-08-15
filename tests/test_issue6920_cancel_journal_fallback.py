"""Regression coverage for #6920 cancel-time run-journal fallback."""

from __future__ import annotations

import copy
import json
import queue
import threading
from unittest.mock import Mock, patch

import pytest

import api.config as config
import api.models as models
import api.streaming as streaming
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


def test_cancel_journal_tool_dedupe_is_turn_scoped():
    """A repeated tool in an older turn must not suppress current recovery."""
    sid = "issue6920_tool_turn_scope"
    stream_id = "stream-tool-turn-scope"
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
    session.tool_calls = [
        {
            "name": "terminal",
            "preview": "ls",
            "snippet": "ls",
            "tid": "old-tool",
            "assistant_msg_idx": 1,
            "done": True,
        }
    ]
    session.pending_started_at = 10
    session.save()
    _start_cancel_state(sid, stream_id)
    RunJournalWriter(sid, stream_id).append_sse_event(
        "tool",
        {
            "name": "terminal",
            "preview": "ls",
            "args": {"cmd": "ls"},
        },
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    current_user_index = next(
        index
        for index, message in enumerate(reloaded.messages)
        if message.get("role") == "user"
        and message.get("content") == "Continue this cancelled turn"
    )
    current_assistant_index = next(
        index
        for index, message in enumerate(reloaded.messages)
        if message.get("role") == "assistant"
        and message.get("_recovered_stream_id") == stream_id
    )
    current_tools = [
        tool_call
        for tool_call in reloaded.tool_calls
        if tool_call.get("_recovered_stream_id") == stream_id
    ]
    assert len(current_tools) == 1
    assert current_tools[0]["assistant_msg_idx"] == current_assistant_index
    assert current_user_index < current_assistant_index
    assert reloaded.messages[current_assistant_index].get("_error") is not True
    assert len(reloaded.tool_calls) == 2
    assert reloaded.tool_calls[0].get("_recovered_stream_id") is None
    assert reloaded.tool_calls[0]["assistant_msg_idx"] == 1

    # Re-reading the same recovered stream remains idempotent.
    from api.models import _append_journaled_partial_output

    assert _append_journaled_partial_output(
        reloaded,
        stream_id,
        dedupe_existing=True,
        mark_partial=True,
        current_turn_start=current_user_index,
    ) is False
    assert len(
        [
            tool_call
            for tool_call in reloaded.tool_calls
            if tool_call.get("_recovered_stream_id") == stream_id
        ]
    ) == 1

    # Callers without the cancellation-only lower bound retain the legacy
    # session-wide match for an untagged core-transcript tool card.
    legacy_session = Session(
        session_id="issue6920_tool_legacy",
        messages=[{"role": "assistant", "content": "Earlier answer"}],
        tool_calls=[{"name": "terminal", "preview": "ls", "snippet": "ls"}],
    )
    assert models._journal_tool_already_present(
        legacy_session,
        "terminal",
        "ls",
        stream_id="legacy-stream",
    ) is True


def test_cancel_journal_tool_dedupe_rejects_invalid_owner_rows():
    """Malformed or marker owners must not suppress a current-turn tool."""
    session = Session(
        session_id="issue6920_tool_invalid_owner",
        messages=[
            {"role": "user", "content": "Current request"},
            {"role": "assistant", "content": "Current progress"},
            {
                "role": "assistant",
                "content": "Task cancelled.",
                "_error": True,
                "type": "interrupted",
            },
        ],
    )
    for owner_index in (999, 2):
        session.tool_calls = [
            {
                "name": "terminal",
                "preview": "ls",
                "assistant_msg_idx": owner_index,
            }
        ]
        assert models._journal_tool_already_present(
            session,
            "terminal",
            "ls",
            stream_id="current-stream",
            min_index=1,
        ) is False

    session.tool_calls = [
        {
            "name": "terminal",
            "preview": "ls",
            "assistant_msg_idx": 1,
        }
    ]
    assert models._journal_tool_already_present(
        session,
        "terminal",
        "ls",
        stream_id="current-stream",
        min_index=1,
    ) is True


def test_recovered_journal_segments_bypass_legacy_partial_collapse():
    """Distinct recovered rows survive the save/load partial cleanup pass."""
    sid = "issue6920_recovered_partial_collapse"
    stream_id = "stream-recovered-partials"
    session = Session(
        session_id=sid,
        messages=[
            {"role": "user", "content": "Continue this cancelled turn", "timestamp": 1},
            {
                "role": "assistant",
                "content": "Repeated recovered progress.",
                "timestamp": 2,
                "_partial": True,
                "_recovered_from_run_journal": True,
                "_recovered_stream_id": stream_id,
            },
            {
                "role": "assistant",
                "content": "Repeated recovered progress.",
                "timestamp": 3,
                "_partial": True,
                "_recovered_from_run_journal": True,
                "_recovered_stream_id": stream_id,
            },
            {
                "role": "assistant",
                "content": "Task cancelled.",
                "timestamp": 4,
                "_error": True,
            },
        ],
        tool_calls=[
            {
                "name": "terminal",
                "preview": "ls",
                "snippet": "ls",
                "tid": "journal-tool-1",
                "assistant_msg_idx": 2,
                "_partial": True,
                "_recovered_from_run_journal": True,
                "_recovered_stream_id": stream_id,
            },
        ],
    )
    session.save()

    reloaded = Session.load(sid)
    assert reloaded is not None
    recovered = [
        row for row in _assistant_rows(reloaded)
        if row.get("_recovered_from_run_journal")
    ]
    assert [row.get("content") for row in recovered] == [
        "Repeated recovered progress.",
        "Repeated recovered progress.",
    ]
    assert len(recovered) == 2
    assert len(reloaded.messages) == 4
    assert reloaded.tool_calls[0]["assistant_msg_idx"] == 2
    assert reloaded.messages[reloaded.tool_calls[0]["assistant_msg_idx"]].get(
        "_recovered_stream_id"
    ) == stream_id
    assert reloaded.messages[reloaded.tool_calls[0]["assistant_msg_idx"]].get(
        "_error"
    ) is not True

    legacy = Session(
        session_id="issue6920_legacy_partial_collapse",
        messages=[
            {"role": "assistant", "content": "Legacy duplicate.", "_partial": True},
            {"role": "assistant", "content": "Legacy duplicate.", "_partial": True},
        ],
    )
    legacy.save()
    legacy_reloaded = Session.load(legacy.session_id)
    assert legacy_reloaded is not None
    assert [
        row for row in _assistant_rows(legacy_reloaded) if row.get("_partial")
    ] == [
        {"role": "assistant", "content": "Legacy duplicate.", "_partial": True},
    ]


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


def test_btw_cancel_recovery_never_mutates_parent_history():
    """A shallow-copied /btw transcript must isolate cancel recovery rows."""
    parent = Session(
        session_id="issue6920_btw_parent",
        title="Parent",
        messages=[
            {"role": "user", "content": "Earlier question", "timestamp": 1},
            {
                "role": "assistant",
                "content": "Repeated parent answer",
                "timestamp": 2,
            },
        ],
        context_messages=[],
    )
    parent.save()
    parent_before = copy.deepcopy(parent.messages)

    stream_id = "stream-btw-parent-isolation"
    ephemeral = Session(
        session_id="issue6920_btw_ephemeral",
        title="btw: side question",
        messages=list(parent.messages),
        context_messages=[],
        parent_session_id=parent.session_id,
        session_source="btw",
    )
    writer = RunJournalWriter(ephemeral.session_id, stream_id)
    writer.append_sse_event("token", {"text": "Repeated parent answer"})
    writer.append_sse_event(
        "reasoning", {"text": "New side-question reasoning"},
    )

    from api.models import _append_journaled_partial_output

    assert _append_journaled_partial_output(
        ephemeral,
        stream_id,
        dedupe_existing=True,
        mark_partial=True,
    ) is True
    assert parent.messages == parent_before
    assert len(ephemeral.messages) == len(parent_before) + 1
    recovered = ephemeral.messages[-1]
    assert recovered["content"] == "Repeated parent answer"
    assert recovered["reasoning"] == "New side-question reasoning"
    assert recovered["_partial"] is True
    assert recovered["_recovered_stream_id"] == stream_id

    assert _append_journaled_partial_output(
        ephemeral,
        stream_id,
        dedupe_existing=True,
        mark_partial=True,
    ) is False
    assert parent.messages == parent_before
    assert len(ephemeral.messages) == len(parent_before) + 1


def test_live_buffer_cancel_partial_is_next_turn_context():
    """A live-buffer partial must be durable in provider context after cancel."""
    sid = "issue6920_live_buffer_context"
    stream_id = "stream-live-buffer-context"
    session = _session(sid, stream_id)
    session.messages = [
        {"role": "user", "content": "Earlier question", "timestamp": 1},
        {"role": "assistant", "content": "Earlier answer", "timestamp": 2},
    ]
    session.context_messages = [dict(message) for message in session.messages]
    session.pending_started_at = 10
    session.save()
    _start_cancel_state(sid, stream_id)
    config.STREAM_PARTIAL_TEXT[stream_id] = "Live partial survives cancellation."
    config.STREAM_REASONING_TEXT[stream_id] = (
        "Display-only live reasoning must stay out of provider context."
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    current_user = "Continue this cancelled turn"
    partial_text = "Live partial survives cancellation."
    message_user_indexes = [
        idx for idx, message in enumerate(reloaded.messages)
        if message.get("role") == "user" and message.get("content") == current_user
    ]
    message_partial_indexes = [
        idx for idx, message in enumerate(reloaded.messages)
        if message.get("role") == "assistant"
        and message.get("_partial") is True
        and message.get("content") == partial_text
    ]
    context_user_indexes = [
        idx for idx, message in enumerate(reloaded.context_messages)
        if message.get("role") == "user" and message.get("content") == current_user
    ]
    context_partial_indexes = [
        idx for idx, message in enumerate(reloaded.context_messages)
        if message.get("role") == "assistant"
        and message.get("_partial") is True
        and message.get("content") == partial_text
    ]
    assert len(message_user_indexes) == 1
    assert len(message_partial_indexes) == 1
    assert message_user_indexes[0] + 1 == message_partial_indexes[0]
    assert len(context_user_indexes) == 1
    assert len(context_partial_indexes) == 1
    assert context_user_indexes[0] + 1 == context_partial_indexes[0]
    assert reloaded.pending_user_message is None
    assert reloaded.pending_attachments == []
    assert reloaded.pending_started_at is None
    next_context = _context_messages_for_new_turn(reloaded, "Next turn")
    next_user_indexes = [
        idx for idx, message in enumerate(next_context)
        if message.get("role") == "user" and message.get("content") == current_user
    ]
    next_partial_indexes = [
        idx for idx, message in enumerate(next_context)
        if message.get("role") == "assistant"
        and message.get("_partial") is True
        and message.get("content") == partial_text
    ]
    assert len(next_user_indexes) == 1
    assert len(next_partial_indexes) == 1
    assert next_user_indexes[0] + 1 == next_partial_indexes[0]
    assert next_context[next_user_indexes[0]]["content"] == current_user
    assert next_context[next_partial_indexes[0]]["content"] == partial_text
    assert "Display-only live reasoning" not in json.dumps(
        reloaded.context_messages, ensure_ascii=False,
    )
    assert "Display-only live reasoning" not in json.dumps(
        next_context, ensure_ascii=False,
    )


def test_repeated_journal_prose_is_turn_scoped_in_context():
    """Equal recovered prose in an older turn cannot own the current segment."""
    sid = "issue6920_repeated_journal_context"
    stream_id = "stream-repeated-journal-context"
    repeated_text = "The same journal prose appears in two turns."
    session = _session(sid, stream_id)
    session.messages = [
        {"role": "user", "content": "Earlier question", "timestamp": 1},
        {"role": "assistant", "content": repeated_text, "timestamp": 2},
    ]
    session.context_messages = [dict(message) for message in session.messages]
    session.pending_started_at = 10
    session.save()
    _start_cancel_state(sid, stream_id)
    RunJournalWriter(sid, stream_id).append_sse_event(
        "token", {"text": repeated_text},
    )

    assert cancel_stream(stream_id) is True

    reloaded = Session.load(sid)
    assert reloaded is not None
    current_message_user_index = next(
        idx for idx, message in enumerate(reloaded.messages)
        if message.get("role") == "user"
        and message.get("content") == "Continue this cancelled turn"
    )
    current_message_rows = [
        (idx, message) for idx, message in enumerate(reloaded.messages)
        if message.get("role") == "assistant"
        and message.get("content") == repeated_text
        and message.get("_recovered_from_run_journal") is True
    ]
    assert len(current_message_rows) == 1
    assert current_message_rows[0][0] == current_message_user_index + 1
    current_user_index = next(
        idx for idx, message in enumerate(reloaded.context_messages)
        if message.get("role") == "user"
        and message.get("content") == "Continue this cancelled turn"
    )
    recovered_rows = [
        (idx, message) for idx, message in enumerate(reloaded.context_messages)
        if message.get("role") == "assistant"
        and message.get("content") == repeated_text
    ]
    assert len(recovered_rows) == 2
    assert recovered_rows[0][0] < current_user_index < recovered_rows[1][0]
    assert recovered_rows[1][0] == current_user_index + 1
    next_context = _context_messages_for_new_turn(reloaded, "Next turn")
    next_user_indexes = [
        (idx, message) for idx, message in enumerate(next_context)
        if message.get("role") == "user"
        and message.get("content") == "Continue this cancelled turn"
    ]
    next_rows = [
        (idx, message) for idx, message in enumerate(next_context)
        if message.get("role") == "assistant"
        and message.get("content") == repeated_text
    ]
    assert len(next_user_indexes) == 1
    assert len(next_rows) == 2
    assert next_rows[1][0] == next_user_indexes[0][0] + 1
    assert next_rows[1][1].get("_recovered_from_run_journal") is True
    from api.models import _append_journaled_partial_output

    assert _append_journaled_partial_output(
        reloaded,
        stream_id,
        dedupe_existing=True,
        mark_partial=True,
        current_turn_start=current_message_user_index,
    ) is False
    assert len([
        message for message in reloaded.messages
        if message.get("role") == "assistant"
        and message.get("content") == repeated_text
        and message.get("_recovered_from_run_journal") is True
    ]) == 1


def test_settlement_preserves_identity_distinct_recovered_segments():
    """Successful settlement keeps equal-text recovered segments by identity."""
    sid = "issue6920_settlement_segments"
    repeated_text = "Recovered progress is intentionally repeated."
    recovered_one = {
        "role": "assistant",
        "content": repeated_text,
        "timestamp": 3,
        "_partial": True,
        "_recovered_from_run_journal": True,
        "_recovered_stream_id": "stream-settlement-segments",
        "_recovered_event_id": "stream-settlement-segments:7",
    }
    recovered_two = {
        **recovered_one,
        "timestamp": 4,
        "_recovered_event_id": "stream-settlement-segments:9",
    }
    previous_messages = [
        {"role": "user", "content": "Earlier question", "timestamp": 1},
        {"role": "assistant", "content": "Earlier answer", "timestamp": 2},
        {"role": "user", "content": "Continue this cancelled turn", "timestamp": 3},
        recovered_one,
        recovered_two,
    ]
    previous_context = copy.deepcopy(previous_messages)
    session = Session(
        session_id=sid,
        title="Settlement segments",
        messages=copy.deepcopy(previous_messages),
        context_messages=copy.deepcopy(previous_context),
    )
    next_user = {"role": "user", "content": "Continue after cancellation", "timestamp": 5}
    sanitized_history = streaming._sanitize_messages_for_agent(previous_context)
    assert all(
        "_recovered_event_id" not in message
        and "_recovered_from_run_journal" not in message
        for message in sanitized_history
    )
    result_messages = sanitized_history + [
        next_user,
        {"role": "assistant", "content": "The next turn is complete."},
    ]

    streaming._settle_result_messages(
        session,
        previous_messages,
        previous_context,
        result_messages,
        next_user["content"],
        "webui",
        None,
    )
    session.save()
    reloaded = Session.load(sid)
    assert reloaded is not None
    recovered_context = [
        message for message in reloaded.context_messages
        if message.get("_recovered_from_run_journal")
    ]
    assert [message.get("_recovered_event_id") for message in recovered_context] == [
        "stream-settlement-segments:7",
        "stream-settlement-segments:9",
    ]
    assert [message.get("content") for message in recovered_context] == [
        repeated_text,
        repeated_text,
    ]
    recovered = [
        message for message in reloaded.messages
        if message.get("_recovered_from_run_journal")
    ]
    assert [message.get("_recovered_event_id") for message in recovered] == [
        "stream-settlement-segments:7",
        "stream-settlement-segments:9",
    ]
    assert [message.get("content") for message in recovered] == [
        repeated_text,
        repeated_text,
    ]
    current_user_index = next(
        idx for idx, message in enumerate(reloaded.messages)
        if message.get("role") == "user"
        and message.get("content") == "Continue this cancelled turn"
    )
    recovered_indexes = [
        idx for idx, message in enumerate(reloaded.messages)
        if message.get("_recovered_from_run_journal")
    ]
    assert recovered_indexes == [current_user_index + 1, current_user_index + 2]
    assert any(
        message.get("role") == "assistant"
        and message.get("content") == "The next turn is complete."
        for message in reloaded.messages
    )


def test_settlement_preserves_recovered_tool_owner_and_summary():
    """Tool-summary rebuild retains a recovered tool and its assistant owner."""
    sid = "issue6920_settlement_tool"
    stream_id = "stream-settlement-tool"
    recovered_assistant = {
        "role": "assistant",
        "content": "Recovered tool activity.",
        "timestamp": 3,
        "_partial": True,
        "_recovered_from_run_journal": True,
        "_recovered_stream_id": stream_id,
        "_recovered_event_id": f"{stream_id}:4",
    }
    recovered_tool = {
        "name": "terminal",
        "preview": "completed output",
        "snippet": "completed output",
        "summary": "completed output",
        "tid": f"{stream_id}:5",
        "assistant_msg_idx": 3,
        "done": True,
        "_recovered_from_run_journal": True,
        "_recovered_stream_id": stream_id,
        "_recovered_event_id": f"{stream_id}:5",
    }
    previous_messages = [
        {"role": "user", "content": "Earlier question", "timestamp": 1},
        {"role": "assistant", "content": "Earlier answer", "timestamp": 2},
        {"role": "user", "content": "Continue this cancelled turn", "timestamp": 3},
        recovered_assistant,
    ]
    previous_context = copy.deepcopy(previous_messages)
    session = Session(
        session_id=sid,
        title="Settlement tool",
        messages=copy.deepcopy(previous_messages),
        context_messages=copy.deepcopy(previous_context),
        tool_calls=[recovered_tool],
    )
    next_user = {"role": "user", "content": "Continue after cancellation", "timestamp": 5}
    streaming._settle_result_messages(
        session,
        previous_messages,
        previous_context,
        copy.deepcopy(previous_context) + [
            next_user,
            {"role": "assistant", "content": "The next turn is complete."},
        ],
        next_user["content"],
        "webui",
        None,
    )
    # Mirror the normal successful-turn tool-summary rebuild in the worker.
    session.tool_calls = streaming._extract_tool_calls_from_messages(
        session.messages,
        live_tool_calls=session.tool_calls,
    )
    session.save()
    reloaded = Session.load(sid)
    assert reloaded is not None
    recovered_tools = [
        tool for tool in reloaded.tool_calls
        if tool.get("_recovered_event_id") == f"{stream_id}:5"
    ]
    assert len(recovered_tools) == 1
    assert recovered_tools[0].get("summary") == "completed output"
    owner_index = recovered_tools[0].get("assistant_msg_idx")
    assert isinstance(owner_index, int)
    assert reloaded.messages[owner_index].get("_recovered_event_id") == f"{stream_id}:4"
    assert reloaded.messages[owner_index].get("_recovered_from_run_journal") is True
