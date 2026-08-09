"""Route-level regression coverage for best-effort run-journal admission.

The run journal must not become a brick-class dependency for starting work.
Only a valid retired authority record blocks admission; corrupt, unreadable, or
unwritable authority degrades the run to an unjournaled execution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api import models, routes, run_journal, turn_journal


class _Session:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.active_stream_id = None
        self.workspace = "/tmp/workspace"
        self.model = "test-model"
        self.model_provider = None
        self.profile = None
        self.messages = []
        self.title = "Untitled"
        self.pending_started_at = 1.0

    def save(self):
        return None


class _Thread:
    created = []

    def __init__(self, *, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon
        self.__class__.created.append(self)

    def start(self):
        return None


def _authority_path(root: Path, session_id: str) -> Path:
    return root / "_run_journal" / ".incarnations" / f"{session_id}.json"


def _install_authority_failure(monkeypatch, root: Path, session_id: str, mode: str):
    path = _authority_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "corrupt":
        path.write_text("{not-json", encoding="ascii")
    elif mode == "unreadable":
        path.write_text("present", encoding="ascii")
        real_read_text = Path.read_text

        def deny_authority_read(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError("authority denied")
            return real_read_text(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", deny_authority_read)
    elif mode == "unwritable":
        def deny_authority_write(_path, _incarnation, *, state):
            raise OSError("authority write denied")

        monkeypatch.setattr(
            run_journal,
            "_write_run_journal_incarnation",
            deny_authority_write,
        )
    elif mode == "retired":
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "session_id": session_id,
                    "state": "retired",
                    "incarnation": "0" * 32,
                }
            ),
            encoding="ascii",
        )
    else:  # pragma: no cover - test helper guard
        raise AssertionError(f"unknown authority mode: {mode}")


@pytest.fixture
def route_harness(monkeypatch, tmp_path):
    _Thread.created = []
    monkeypatch.setattr(run_journal, "_default_session_dir", lambda: tmp_path)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_kwargs: None)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "_active_run_stream_for_session", lambda _sid: None)
    monkeypatch.setattr(routes, "_is_hidden_empty_session", lambda _session: False)
    monkeypatch.setattr(routes, "set_last_workspace", lambda _workspace: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: object())
    monkeypatch.setattr(routes, "register_stream_owner", lambda *_args: None)
    monkeypatch.setattr(routes, "register_session_writeback_owner", lambda *_args: None)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "STREAMS", {})
    monkeypatch.setattr(routes.threading, "Thread", _Thread)
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(turn_journal, "append_turn_journal_event", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        routes,
        "_prepare_chat_start_session_for_stream",
        lambda session, **kwargs: setattr(session, "active_stream_id", kwargs["stream_id"]),
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda _handler, payload, status=200, **_kwargs: {**payload, "_status": status},
    )
    return tmp_path


@pytest.mark.parametrize("mode", ["corrupt", "unreadable", "unwritable"])
def test_send_degrades_authority_failure_to_unjournaled_execution(
    route_harness, monkeypatch, mode
):
    session = _Session("send-authority")
    _install_authority_failure(monkeypatch, route_harness, session.session_id, mode)

    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace=session.workspace,
        model=session.model,
        external_runtime_owned=False,
    )

    assert response.get("_status", 200) == 200
    assert _Thread.created[-1].kwargs["run_journal_incarnation"] is None


def test_send_valid_retired_authority_still_blocks(route_harness, monkeypatch):
    session = _Session("send-retired")
    _install_authority_failure(monkeypatch, route_harness, session.session_id, "retired")

    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace=session.workspace,
        model=session.model,
        external_runtime_owned=False,
    )

    assert response["_status"] == 409
    assert response["type"] == "run_journal_authority_unavailable"
    assert not _Thread.created


@pytest.mark.parametrize("endpoint", ["btw", "background"])
@pytest.mark.parametrize("mode", ["corrupt", "unreadable", "unwritable"])
def test_auxiliary_route_degrades_authority_failure_to_unjournaled_execution(
    route_harness, monkeypatch, endpoint, mode
):
    parent = _Session(f"{endpoint}-parent")
    child = _Session(f"{endpoint}-child")
    monkeypatch.setattr(routes, "get_session", lambda _sid: parent)
    monkeypatch.setattr(models, "new_session", lambda **_kwargs: child)
    _install_authority_failure(monkeypatch, route_harness, child.session_id, mode)

    if endpoint == "btw":
        from api import background

        monkeypatch.setattr(background, "track_btw", lambda *_args: None)
        response = routes._handle_btw(
            object(), {"session_id": parent.session_id, "question": "side question"}
        )
    else:
        from api import background

        monkeypatch.setattr(background, "track_background", lambda *_args: None)
        monkeypatch.setattr(background, "complete_background", lambda *_args: None)
        response = routes._handle_background(
            object(), {"session_id": parent.session_id, "prompt": "background task"}
        )

    assert response["_status"] == 200
    assert _Thread.created[-1].kwargs["run_journal_incarnation"] is None


@pytest.mark.parametrize("endpoint", ["btw", "background"])
def test_auxiliary_route_valid_retired_authority_still_blocks(
    route_harness, monkeypatch, endpoint
):
    parent = _Session(f"{endpoint}-parent-retired")
    child = _Session(f"{endpoint}-child-retired")
    monkeypatch.setattr(routes, "get_session", lambda _sid: parent)
    monkeypatch.setattr(models, "new_session", lambda **_kwargs: child)
    _install_authority_failure(monkeypatch, route_harness, child.session_id, "retired")

    if endpoint == "btw":
        response = routes._handle_btw(
            object(), {"session_id": parent.session_id, "question": "side question"}
        )
    else:
        response = routes._handle_background(
            object(), {"session_id": parent.session_id, "prompt": "background task"}
        )

    assert response["_status"] == 409
    assert response["type"] == "run_journal_authority_unavailable"
    assert not _Thread.created


@pytest.mark.parametrize("endpoint", ["send", "btw", "background"])
def test_unexpected_activation_bug_is_not_silenced(
    route_harness, monkeypatch, endpoint
):
    parent = _Session(f"{endpoint}-parent-bug")
    child = _Session(f"{endpoint}-child-bug")
    monkeypatch.setattr(routes, "get_session", lambda _sid: parent)
    monkeypatch.setattr(models, "new_session", lambda **_kwargs: child)
    monkeypatch.setattr(
        run_journal,
        "activate_run_journal_session",
        lambda _sid: (_ for _ in ()).throw(ValueError("unexpected programmer bug")),
    )
    monkeypatch.setattr(
        run_journal,
        "validate_run_journal_session_activation",
        lambda _sid: None,
    )

    with pytest.raises(ValueError, match="unexpected programmer bug"):
        if endpoint == "send":
            routes._start_chat_stream_for_session(
                parent,
                msg="hello",
                workspace=parent.workspace,
                model=parent.model,
                external_runtime_owned=False,
            )
        elif endpoint == "btw":
            routes._handle_btw(
                object(), {"session_id": parent.session_id, "question": "side question"}
            )
        else:
            routes._handle_background(
                object(), {"session_id": parent.session_id, "prompt": "background task"}
            )
