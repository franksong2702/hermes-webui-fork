"""Regression coverage for #6751 api_content replay preservation.

The fixture models the durable Agent ``messages`` rows that carry a clean
visible ``content`` value plus the exact provider-facing ``api_content``
sidecar.  The two user rows intentionally render the same text while keeping
different timestamps and sidecars so a content-only reconciliation can be
observed if it crosses the rows.
"""

import sqlite3
import sys
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.requires_agent_modules


def _write_state_db(path, sid):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL,
            api_content TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO messages (session_id, role, content, timestamp, api_content)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (sid, "user", "same visible text", 100.0, "[Workspace::v1: /one]\\nsame visible text"),
            (sid, "user", "same visible text", 200.0, "[Workspace::v1: /two]\\nsame visible text"),
        ],
    )
    conn.commit()
    conn.close()


def test_issue6751_state_db_sidecars_survive_reconcile_and_internal_agent_projection(
    monkeypatch, tmp_path
):
    """Distinct same-text rows retain their own sidecar through every boundary."""
    import api.models as models
    from api.streaming import _sanitize_messages_for_agent

    sid = "issue6751-replay"
    db_path = tmp_path / "state.db"
    _write_state_db(db_path, sid)
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    state_messages = models.get_state_db_session_messages(sid)
    assert [message["api_content"] for message in state_messages] == [
        "[Workspace::v1: /one]\\nsame visible text",
        "[Workspace::v1: /two]\\nsame visible text",
    ]
    assert [message["_state_db_row_id"] for message in state_messages] == [1, 2]

    sidecar_messages = [
        {"role": "user", "content": "same visible text", "timestamp": 100.0},
        {"role": "user", "content": "same visible text", "timestamp": 200.0},
    ]
    merged = models.merge_session_messages_append_only(sidecar_messages, state_messages)
    assert [message.get("api_content") for message in merged] == [
        "[Workspace::v1: /one]\\nsame visible text",
        "[Workspace::v1: /two]\\nsame visible text",
    ]

    internal_history = _sanitize_messages_for_agent(merged)
    assert [message.get("api_content") for message in internal_history] == [
        "[Workspace::v1: /one]\\nsame visible text",
        "[Workspace::v1: /two]\\nsame visible text",
    ]


def test_issue6751_reconciliation_prioritizes_row_id_and_preserves_existing_sidecar():
    from api.models import merge_session_messages_append_only

    sidecar = [
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 200.0,
            "_state_db_row_id": 2,
            "api_content": "already-authoritative",
        },
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 100.0,
            "_state_db_row_id": 1,
        },
    ]
    state = [
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 100.0,
            "_state_db_row_id": 1,
            "api_content": "[Workspace::v1: /one]\\nsame visible text",
        },
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 200.0,
            "_state_db_row_id": 2,
            "api_content": "[Workspace::v1: /two]\\nsame visible text",
        },
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert [message["api_content"] for message in merged] == [
        "already-authoritative",
        "[Workspace::v1: /one]\\nsame visible text",
    ]


def test_issue6751_reconciliation_consumes_each_state_source_once_on_timestamp_collision():
    from api.models import merge_session_messages_append_only

    sidecar = [
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 100.0,
            "_state_db_row_id": 1,
        },
        {"role": "user", "content": "same visible text", "timestamp": 100.0},
    ]
    state = [
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 100.0,
            "_state_db_row_id": 1,
            "api_content": "wire-one",
        },
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 100.0,
            "api_content": "wire-two",
        },
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert [message.get("api_content") for message in merged] == ["wire-one", "wire-two"]


def test_issue6751_reconciliation_fails_closed_when_row_id_roles_disagree():
    from api.models import merge_session_messages_append_only

    sidecar = [
        {
            "role": "user",
            "content": "same visible text",
            "timestamp": 100.0,
            "_state_db_row_id": 1,
        }
    ]
    state = [
        {
            "role": "assistant",
            "content": "same visible text",
            "timestamp": 100.0,
            "_state_db_row_id": 1,
            "api_content": "assistant-wire",
        }
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert "api_content" not in merged[0]


def test_issue6751_ambiguous_metadata_free_sequence_fails_closed():
    from api.models import merge_session_messages_append_only

    sidecar = [
        {"role": "assistant", "content": "same answer"},
        {"role": "assistant", "content": "same answer"},
    ]
    state = [
        {"role": "assistant", "content": "same answer", "api_content": "wire-one"},
        {"role": "assistant", "content": "same answer", "api_content": "wire-two"},
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert [message.get("api_content") for message in merged] == [None, None]


def test_issue6751_partial_metadata_bucket_fails_closed_instead_of_zipping():
    from api.models import merge_session_messages_append_only

    sidecar = [
        {"role": "assistant", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    state = [
        {"role": "assistant", "content": "first", "timestamp": 10.0, "api_content": "wire-first"},
        {"role": "assistant", "content": "second", "api_content": "wire-second"},
    ]

    merged = merge_session_messages_append_only(sidecar, state)

    assert [message.get("api_content") for message in merged] == [None, None]


def test_issue6751_state_db_reader_preserves_shape_without_api_content(monkeypatch, tmp_path):
    import api.models as models

    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL)"
    )
    conn.execute(
        "INSERT INTO messages VALUES (7, ?, ?, ?, ?)",
        ("shape-session", "user", "ordinary", 1.0),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(models, "_active_state_db_path", lambda: db_path)

    assert models.get_state_db_session_messages("shape-session") == [
        {"role": "user", "content": "ordinary", "timestamp": 1.0}
    ]


def test_issue6751_public_message_projection_strips_internal_replay_fields(monkeypatch):
    import api.helpers as helpers
    import api.config as config

    monkeypatch.setattr(config, "load_settings", lambda: {"api_redact_enabled": False})
    safe = helpers.redact_session_data(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "visible",
                    "api_content": "wire",
                    "_state_db_row_id": 7,
                    "_db_row_id": 8,
                    "state_db_row_id": 9,
                }
            ]
        }
    )

    assert safe["messages"] == [{"role": "user", "content": "visible"}]


def test_issue6751_provider_projection_still_strips_api_content_by_default():
    from api.streaming import _sanitize_messages_for_api

    sanitized = _sanitize_messages_for_api(
        [{
            "role": "user",
            "content": "same visible text",
            "api_content": "[Workspace::v1: /tmp]\\nsame visible text",
        }]
    )

    assert sanitized == [{"role": "user", "content": "same visible text"}]


def test_issue6751_sync_chat_agent_receives_original_api_content_bytes(monkeypatch, tmp_path):
    """The production sync route must hand the Agent the original wire text."""
    import api.config as config
    import api.models as models
    import api.routes as routes

    state_dir = tmp_path / "state"
    session_dir = state_dir / "sessions"
    session_dir.mkdir(parents=True)
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", state_dir / "session_index.json")
    monkeypatch.setattr(routes, "get_session", models.get_session)
    monkeypatch.setattr(routes, "title_from", models.title_from)
    monkeypatch.setattr(config, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
    monkeypatch.setattr(routes, "get_config", lambda: {"model": "test-model", "provider": "test-provider"})
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value: tmp_path)
    monkeypatch.setattr(routes, "load_settings", lambda: {})
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda: [])

    original_wire = "[Workspace::v1: /original-workspace]\nturn-two request"
    context = [
        {
            "role": "user",
            "content": "turn-two request",
            "timestamp": 2.0,
            "api_content": original_wire,
        },
        {"role": "assistant", "content": "turn-two answer", "timestamp": 3.0},
    ]
    session = models.Session(
        session_id="issue6751_sync_agent",
        workspace=str(tmp_path),
        messages=list(context),
        context_messages=list(context),
        model="test-model",
        model_provider="test-provider",
    )
    session.save(touch_updated_at=False)

    captured = {}

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **kwargs):
            captured["history"] = list(kwargs.get("conversation_history") or [])
            return {
                "messages": captured["history"]
                + [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "ok"},
                ],
                "final_response": "ok",
                "completed": True,
            }

    monkeypatch.setitem(sys.modules, "run_agent", SimpleNamespace(AIAgent=FakeAgent))

    class Handler:
        def __init__(self):
            self.headers = {}
            self.status = None
            self.wfile = SimpleNamespace(write=lambda _body: None)

        def send_response(self, status):
            self.status = status

        def send_header(self, _name, _value):
            return None

        def end_headers(self):
            return None

    handler = Handler()
    routes._handle_chat_sync(
        handler,
        {
            "session_id": session.session_id,
            "message": "follow-up",
            "workspace": str(tmp_path),
        },
    )

    assert handler.status == 200
    assert [message.get("api_content") for message in captured["history"] if message.get("role") == "user"] == [
        original_wire
    ]
