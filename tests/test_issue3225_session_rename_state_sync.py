"""Regression coverage for #3225 — /api/session/rename should mirror title
updates to state.db when sync_to_insights is enabled."""

from types import SimpleNamespace

import api.routes as routes
import api.state_sync as state_sync


def _capture_post(monkeypatch, body):
    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        )
        or True,
    )
    return captured


def _build_session():
    session = SimpleNamespace(
        session_id="s-3225-sync-title",
        title="Old Title",
        profile="demo-profile",
        messages=[{"role": "user", "content": "hello"}],
        model="openai/gpt-4o",
        input_tokens=12,
        output_tokens=34,
        estimated_cost=0.12,
        save=lambda: True,
    )

    def _compact():
        return {
            "session_id": "s-3225-sync-title",
            "title": session.title,
        }

    session.compact = _compact
    return session


def test_session_rename_syncs_title_to_state_db_when_enabled(monkeypatch):
    monkeypatch.setattr(routes, "load_settings", lambda: {"sync_to_insights": True})
    calls = []

    def fake_sync_session_usage(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(state_sync, "sync_session_usage", fake_sync_session_usage)
    captured = _capture_post(monkeypatch, {"session_id": "s-3225-sync-title", "title": "New Title"})

    session = _build_session()
    monkeypatch.setattr(routes, "get_session", lambda sid: session)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/rename")) is True
    assert captured["status"] == 200
    assert session.title == "New Title"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ()
    assert kwargs["session_id"] == "s-3225-sync-title"
    assert kwargs["title"] == "New Title"
    assert kwargs["input_tokens"] == 12
    assert kwargs["output_tokens"] == 34
    assert kwargs["estimated_cost"] == 0.12
    assert kwargs["model"] == "openai/gpt-4o"
    assert kwargs["message_count"] == 1
    assert kwargs["profile"] == "demo-profile"
    assert captured["payload"]["session"]["title"] == "New Title"


def test_session_rename_skips_state_db_sync_when_disabled(monkeypatch):
    monkeypatch.setattr(routes, "load_settings", lambda: {"sync_to_insights": False})
    called = {"sync": 0}

    def fake_sync_session_usage(*_args, **_kwargs):
        called["sync"] += 1

    monkeypatch.setattr(state_sync, "sync_session_usage", fake_sync_session_usage)
    captured = _capture_post(monkeypatch, {"session_id": "s-3225-sync-title", "title": "New Title"})

    session = _build_session()
    monkeypatch.setattr(routes, "get_session", lambda sid: session)

    assert routes.handle_post(object(), SimpleNamespace(path="/api/session/rename")) is True
    assert captured["status"] == 200
    assert called["sync"] == 0
