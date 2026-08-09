"""Route-level regression coverage for best-effort run-journal admission.

The run journal must not become a brick-class dependency for starting work.
Only a valid retired authority record blocks admission; corrupt, unreadable, or
unwritable authority degrades the run to an unjournaled execution.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from api import models, routes, run_journal, turn_journal


_ORIGINAL_PREPARE_CHAT_START = routes._prepare_chat_start_session_for_stream


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


class _PersistentSession(_Session):
    """Small sidecar stand-in that keeps each save observable."""

    _STATE_FIELDS = (
        "active_stream_id",
        "pending_user_message",
        "pending_attachments",
        "pending_started_at",
        "pending_user_source",
        "title",
        "messages",
        "truncation_watermark",
        "workspace",
        "model",
        "model_provider",
        "post_compression_context_tokens_estimate",
        "updated_at",
    )

    def __init__(self, session_id: str):
        super().__init__(session_id)
        self.active_stream_id = None
        self.pending_user_message = "old pending"
        self.pending_attachments = [{"name": "old.txt"}]
        self.pending_started_at = 42.0
        self.pending_user_source = "old-source"
        self.title = "Untitled"
        self.messages = [{"role": "assistant", "content": "before"}]
        self.truncation_watermark = 123.0
        self.workspace = "/old/workspace"
        self.model = "old-model"
        self.model_provider = "old-provider"
        self.post_compression_context_tokens_estimate = 77
        self.updated_at = 10.0
        self.save_calls = []
        self.fail_save_call = None
        self.persisted = self.snapshot()

    def snapshot(self):
        return {
            field: copy.deepcopy(getattr(self, field, None))
            for field in self._STATE_FIELDS
        }

    def save(self, touch_updated_at=True, **_kwargs):
        if self.fail_save_call is not None and len(self.save_calls) + 1 == self.fail_save_call:
            raise OSError("session rollback write failed")
        if touch_updated_at:
            self.updated_at = 100.0 + len(self.save_calls)
        persisted = self.snapshot()
        self.save_calls.append(persisted)
        self.persisted = copy.deepcopy(persisted)


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


@pytest.fixture
def real_session_store(route_harness, monkeypatch):
    session_dir = route_harness / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    original_sessions = list(models.SESSIONS.items())
    models.SESSIONS.clear()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir, raising=False)
    monkeypatch.setattr(routes, "SESSION_INDEX_FILE", index_file, raising=False)
    yield session_dir, index_file
    models.SESSIONS.clear()
    models.SESSIONS.update(original_sessions)


def _use_real_writeback_owner_registry(monkeypatch):
    owners = {}
    monkeypatch.setattr(
        routes,
        "register_session_writeback_owner",
        lambda session_id, stream_id: owners.__setitem__(session_id, stream_id),
    )
    monkeypatch.setattr(routes, "session_writeback_owner", lambda session_id: owners.get(session_id))
    monkeypatch.setattr(
        routes,
        "clear_session_writeback_owner_if_owned",
        lambda session_id, stream_id: owners.pop(session_id, None)
        if owners.get(session_id) == stream_id
        else None,
    )
    return owners


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


@pytest.mark.parametrize("save_mode", ["deferred", "eager"])
def test_retired_activation_rolls_back_prepared_stream_and_allows_retry(
    route_harness, monkeypatch, save_mode
):
    """A retired authority after prepare must leave no abandoned pending turn."""
    session = _PersistentSession(f"send-retired-after-prepare-{save_mode}")
    before = session.snapshot()
    owners = {}

    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", _ORIGINAL_PREPARE_CHAT_START)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: save_mode)
    monkeypatch.setattr(routes, "_provisional_title_from_prompt", lambda *_args, **_kwargs: "Prompt title")
    monkeypatch.setattr(
        routes,
        "register_session_writeback_owner",
        lambda session_id, stream_id: owners.__setitem__(session_id, stream_id),
    )
    monkeypatch.setattr(
        routes,
        "clear_session_writeback_owner_if_owned",
        lambda session_id, stream_id: owners.pop(session_id, None)
        if owners.get(session_id) == stream_id
        else None,
    )
    monkeypatch.setattr(run_journal, "validate_run_journal_session_activation", lambda _sid: None)
    activation_results = [
        run_journal.RunJournalRetiredAuthorityError("retired"),
        "incarnation-after-retry",
    ]

    def activate(_sid):
        result = activation_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(run_journal, "activate_run_journal_session", activate)

    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert response["_status"] == 409
    assert response["type"] == "run_journal_authority_unavailable"
    assert session.snapshot() == before
    assert session.persisted == before
    assert owners == {}

    retry = routes._start_chat_stream_for_session(
        session,
        msg="hello again",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert retry["stream_id"]
    assert retry["session_id"] == session.session_id
    assert len(_Thread.created) == 1


def test_retired_activation_rollback_failure_fails_closed(route_harness, monkeypatch):
    session = _PersistentSession("send-retired-rollback-failure")
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", _ORIGINAL_PREPARE_CHAT_START)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    monkeypatch.setattr(routes, "_provisional_title_from_prompt", lambda *_args, **_kwargs: "Prompt title")
    monkeypatch.setattr(run_journal, "validate_run_journal_session_activation", lambda _sid: None)
    monkeypatch.setattr(
        run_journal,
        "activate_run_journal_session",
        lambda _sid: (_ for _ in ()).throw(
            run_journal.RunJournalRetiredAuthorityError("retired")
        ),
    )
    # Prepare performs save #1; force the compensating persistence write to fail.
    session.fail_save_call = 2

    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert response["_status"] != 409
    assert response.get("retryable") is not True
    assert response["type"] == "run_journal_authority_rollback_failed"


@pytest.mark.parametrize("rotation", ["active_stream", "writeback_owner"])
def test_retired_rollback_does_not_clobber_successor_rotation(
    route_harness, monkeypatch, rotation
):
    session = _PersistentSession(f"send-retired-successor-{rotation}")
    owners = {}
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", _ORIGINAL_PREPARE_CHAT_START)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "deferred")
    monkeypatch.setattr(routes, "_provisional_title_from_prompt", lambda *_args, **_kwargs: "Prompt title")
    monkeypatch.setattr(
        routes,
        "register_session_writeback_owner",
        lambda session_id, stream_id: owners.__setitem__(session_id, stream_id),
    )
    monkeypatch.setattr(routes, "session_writeback_owner", lambda session_id: owners.get(session_id))
    monkeypatch.setattr(
        routes,
        "clear_session_writeback_owner_if_owned",
        lambda session_id, stream_id: owners.pop(session_id, None)
        if owners.get(session_id) == stream_id
        else None,
    )
    monkeypatch.setattr(run_journal, "validate_run_journal_session_activation", lambda _sid: None)

    def activate(_sid):
        successor_stream_id = "successor-stream"
        if rotation == "active_stream":
            session.active_stream_id = successor_stream_id
        owners[session.session_id] = successor_stream_id
        session.pending_user_message = "successor pending"
        session.save(touch_updated_at=False)
        raise run_journal.RunJournalRetiredAuthorityError("retired")

    monkeypatch.setattr(run_journal, "activate_run_journal_session", activate)

    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert response["_status"] == 500
    assert response["type"] == "run_journal_authority_rollback_failed"
    assert owners[session.session_id] == "successor-stream"
    assert session.pending_user_message == "successor pending"
    if rotation == "active_stream":
        assert session.active_stream_id == "successor-stream"
    else:
        assert session.active_stream_id
    assert session.persisted["pending_user_message"] == "successor pending"


def _configure_retired_activation_after_real_prepare(monkeypatch):
    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", _ORIGINAL_PREPARE_CHAT_START)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "eager")
    monkeypatch.setattr(routes, "_provisional_title_from_prompt", lambda *_args, **_kwargs: "Prompt title")
    monkeypatch.setattr(run_journal, "validate_run_journal_session_activation", lambda _sid: None)
    monkeypatch.setattr(
        run_journal,
        "activate_run_journal_session",
        lambda _sid: (_ for _ in ()).throw(
            run_journal.RunJournalRetiredAuthorityError("retired")
        ),
    )


def test_real_eager_rollback_restores_sidecar_and_existing_backup(
    real_session_store, monkeypatch
):
    _configure_retired_activation_after_real_prepare(monkeypatch)
    _use_real_writeback_owner_registry(monkeypatch)
    session = models.Session(
        session_id="real-eager-existing",
        title="Existing title",
        workspace="/old/workspace",
        model="old-model",
        model_provider="old-provider",
        messages=[{"role": "assistant", "content": "before"}],
        pending_user_message="old pending",
        pending_attachments=[{"name": "old.txt"}],
        pending_started_at=42.0,
        pending_user_source="old-source",
        truncation_watermark=123.0,
        post_compression_context_tokens_estimate=77,
    )
    session.save(touch_updated_at=False)
    sidecar_before = session.path.read_bytes()
    backup_before = b'{"preexisting":true}'
    backup_path = session.path.with_suffix(".json.bak")
    backup_path.write_bytes(backup_before)

    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert response["_status"] == 409
    assert session.path.read_bytes() == sidecar_before
    assert backup_path.read_bytes() == backup_before
    index_rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
    row = next(item for item in index_rows if item["session_id"] == session.session_id)
    assert row["title"] == "Existing title"
    assert row["message_count"] == 1


def test_real_eager_rollback_removes_fresh_sidecar_and_backup(
    real_session_store, monkeypatch
):
    _configure_retired_activation_after_real_prepare(monkeypatch)
    _use_real_writeback_owner_registry(monkeypatch)
    session = models.Session(session_id="real-eager-fresh", title="Untitled", messages=[])
    assert not session.path.exists()
    backup_path = session.path.with_suffix(".json.bak")

    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert response["_status"] == 409
    assert not session.path.exists()
    assert not backup_path.exists()
    assert json.loads(real_session_store[1].read_text(encoding="utf-8")) == []


def test_real_rollback_refuses_external_sidecar_rotation(real_session_store, monkeypatch):
    _configure_retired_activation_after_real_prepare(monkeypatch)
    _use_real_writeback_owner_registry(monkeypatch)
    session = models.Session(
        session_id="real-eager-rotated",
        title="Existing title",
        messages=[{"role": "assistant", "content": "before"}],
    )
    session.save(touch_updated_at=False)
    backup_path = session.path.with_suffix(".json.bak")

    def rotate_then_retire(_sid):
        session.path.write_bytes(b"external-sidecar-rotation")
        raise run_journal.RunJournalRetiredAuthorityError("retired")

    monkeypatch.setattr(run_journal, "activate_run_journal_session", rotate_then_retire)
    response = routes._start_chat_stream_for_session(
        session,
        msg="hello",
        workspace="/new/workspace",
        model="new-model",
        model_provider="new-provider",
        external_runtime_owned=False,
    )

    assert response["_status"] == 500
    assert response["type"] == "run_journal_authority_rollback_failed"
    assert session.path.read_bytes() == b"external-sidecar-rotation"
    assert not backup_path.exists()


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
    models.SESSIONS[child.session_id] = child
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


def _invoke_auxiliary_route(monkeypatch, endpoint, parent, *, trackers):
    from api import background

    if endpoint == "btw":
        monkeypatch.setattr(
            background,
            "track_btw",
            lambda *args: trackers.append(("btw", args)),
        )
        return routes._handle_btw(
            object(), {"session_id": parent.session_id, "question": "side question"}
        )
    monkeypatch.setattr(
        background,
        "track_background",
        lambda *args: trackers.append(("background", args)),
    )
    monkeypatch.setattr(background, "complete_background", lambda *_args: None)
    return routes._handle_background(
        object(), {"session_id": parent.session_id, "prompt": "background task"}
    )


def _install_retired_authority_for_child(root, child):
    path = _authority_path(root, child.session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "session_id": child.session_id,
                "state": "retired",
                "incarnation": "0" * 32,
            }
        ),
        encoding="ascii",
    )
    return path, path.read_bytes()


@pytest.mark.parametrize("endpoint", ["btw", "background"])
def test_auxiliary_retired_authority_removes_fresh_child_exactly(
    real_session_store, monkeypatch, endpoint
):
    parent = _Session(f"{endpoint}-parent-fresh-cleanup")
    parent_before = copy.deepcopy(parent.__dict__)
    created = []
    authority = {}
    real_new_session = models.new_session

    def new_child(**kwargs):
        child = real_new_session(**kwargs)
        created.append(child)
        authority["path"], authority["before"] = _install_retired_authority_for_child(
            real_session_store[0].parent,
            child,
        )
        return child

    monkeypatch.setattr(routes, "get_session", lambda _sid: parent)
    monkeypatch.setattr(models, "new_session", new_child)
    trackers = []

    response = _invoke_auxiliary_route(
        monkeypatch,
        endpoint,
        parent,
        trackers=trackers,
    )

    child = created[0]
    backup_path = child.path.with_suffix(".json.bak")
    assert response["_status"] == 409
    assert response["type"] == "run_journal_authority_unavailable"
    assert not child.path.exists()
    assert not backup_path.exists()
    if real_session_store[1].exists():
        rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
        assert all(row.get("session_id") != child.session_id for row in rows)
    assert child.session_id not in models.SESSIONS
    assert authority["path"].read_bytes() == authority["before"]
    assert not _Thread.created
    assert trackers == []
    assert parent.__dict__ == parent_before


@pytest.mark.parametrize("endpoint", ["btw", "background"])
@pytest.mark.parametrize(
    "rotation",
    ["sidecar", "backup", "owner", "index_missing", "index_duplicate", "index_rotated"],
)
def test_auxiliary_retired_authority_rotation_fails_closed_without_clobbering(
    real_session_store, monkeypatch, endpoint, rotation
):
    parent = _Session(f"{endpoint}-parent-rotation-{rotation}")
    created = []
    authority = {}
    real_new_session = models.new_session
    successor = object()

    def new_child(**kwargs):
        child = real_new_session(**kwargs)
        created.append(child)
        authority["path"], authority["before"] = _install_retired_authority_for_child(
            real_session_store[0].parent,
            child,
        )
        return child

    def rotate_then_retire(session_id):
        child = created[0]
        authority["post_sidecar"] = child.path.read_bytes()
        authority["post_index"] = real_session_store[1].read_bytes()
        if rotation == "sidecar":
            child.path.write_bytes(b"external-sidecar-successor")
        elif rotation == "backup":
            child.path.with_suffix(".json.bak").write_bytes(b"external-backup-successor")
        elif rotation == "owner":
            models.SESSIONS[session_id] = successor
        else:
            rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
            if rotation == "index_missing":
                real_session_store[1].unlink()
            elif rotation == "index_duplicate":
                rows.append(next(row for row in rows if row.get("session_id") == child.session_id))
                real_session_store[1].write_text(
                    json.dumps(rows, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            else:
                next(row for row in rows if row.get("session_id") == child.session_id)["title"] = "rotated child row"
                real_session_store[1].write_text(
                    json.dumps(rows, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        raise run_journal.RunJournalRetiredAuthorityError("retired")

    monkeypatch.setattr(routes, "get_session", lambda _sid: parent)
    monkeypatch.setattr(models, "new_session", new_child)
    monkeypatch.setattr(run_journal, "activate_run_journal_session", rotate_then_retire)
    trackers = []

    response = _invoke_auxiliary_route(
        monkeypatch,
        endpoint,
        parent,
        trackers=trackers,
    )

    child = created[0]
    assert response["_status"] == 500
    assert response["type"] == "run_journal_authority_rollback_failed"
    assert response["retryable"] is False
    if rotation == "sidecar":
        assert child.path.read_bytes() == b"external-sidecar-successor"
    elif rotation == "backup":
        assert child.path.with_suffix(".json.bak").read_bytes() == b"external-backup-successor"
    elif rotation == "owner":
        assert models.SESSIONS[child.session_id] is successor
        assert child.path.read_bytes() == authority["post_sidecar"]
    elif rotation == "index_missing":
        assert not real_session_store[1].exists()
        assert child.path.read_bytes() == authority["post_sidecar"]
    elif rotation == "index_duplicate":
        rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
        assert sum(row.get("session_id") == child.session_id for row in rows) == 2
        assert child.path.read_bytes() == authority["post_sidecar"]
    elif rotation == "index_rotated":
        rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
        child_rows = [row for row in rows if row.get("session_id") == child.session_id]
        assert len(child_rows) == 1
        assert child_rows[0]["title"] == "rotated child row"
        assert child.path.read_bytes() == authority["post_sidecar"]
    else:
        assert real_session_store[1].read_bytes() == authority["post_index"]
    if rotation != "owner":
        assert models.SESSIONS[child.session_id] is child
    assert authority["path"].read_bytes() == authority["before"]
    assert not _Thread.created
    assert trackers == []


@pytest.mark.parametrize("endpoint", ["btw", "background"])
def test_auxiliary_rollback_prunes_only_child_index_row(
    real_session_store, monkeypatch, endpoint
):
    parent = _Session(f"{endpoint}-parent-index-successor")
    sibling = models.Session(
        session_id="sibling-index-row",
        title="Sibling before",
        messages=[{"role": "assistant", "content": "existing"}],
    )
    sibling.save(touch_updated_at=False)
    created = []
    real_new_session = models.new_session

    def new_child(**kwargs):
        child = real_new_session(**kwargs)
        created.append(child)
        _install_retired_authority_for_child(real_session_store[0].parent, child)
        return child

    def update_index_then_retire(_sid):
        rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
        for row in rows:
            if row.get("session_id") == sibling.session_id:
                row["title"] = "Sibling updated concurrently"
        rows.append(
            {
                "session_id": "new-concurrent-row",
                "title": "Concurrent new row",
                "message_count": 0,
            }
        )
        real_session_store[1].write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise run_journal.RunJournalRetiredAuthorityError("retired")

    monkeypatch.setattr(routes, "get_session", lambda _sid: parent)
    monkeypatch.setattr(models, "new_session", new_child)
    monkeypatch.setattr(run_journal, "activate_run_journal_session", update_index_then_retire)
    trackers = []

    response = _invoke_auxiliary_route(
        monkeypatch,
        endpoint,
        parent,
        trackers=trackers,
    )

    child = created[0]
    rows = json.loads(real_session_store[1].read_text(encoding="utf-8"))
    rows_by_id = {row["session_id"]: row for row in rows}
    assert response["_status"] == 409
    assert response["type"] == "run_journal_authority_unavailable"
    assert child.session_id not in rows_by_id
    assert rows_by_id[sibling.session_id]["title"] == "Sibling updated concurrently"
    assert rows_by_id["new-concurrent-row"]["title"] == "Concurrent new row"
    assert not child.path.exists()
    assert child.session_id not in models.SESSIONS
    assert not _Thread.created
    assert trackers == []


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
