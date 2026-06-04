"""Regression tests for issue #3548: closed SessionDB handles are not reused on self-heal retry."""

from __future__ import annotations

from pathlib import Path
import sys
import types
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
STREAMING_PY = (REPO_ROOT / "api" / "streaming.py").read_text(encoding="utf-8")


def test_session_db_helper_uses_request_state_db_path():
    import api.streaming as streaming

    calls = {}

    class FakeSessionDB:
        def __init__(self, db_path=None):
            calls["db_path"] = db_path

        def close(self):
            calls["closed"] = True

    fake_state = types.ModuleType("hermes_state")
    fake_state.SessionDB = FakeSessionDB

    with mock.patch.dict(sys.modules, {"hermes_state": fake_state}):
        state_db_path = Path("/tmp/profile") / "state.db"
        db = streaming._build_session_db_for_stream(state_db_path)

    assert db is not None
    assert calls["db_path"] == state_db_path
    assert isinstance(db, FakeSessionDB)


def test_session_db_helper_returns_none_when_constructor_fails():
    import api.streaming as streaming

    def failing_session_db(db_path=None):
        raise RuntimeError("SessionDB unavailable")

    fake_state = types.ModuleType("hermes_state")
    fake_state.SessionDB = mock.Mock(side_effect=failing_session_db)

    with mock.patch.dict(sys.modules, {"hermes_state": fake_state}):
        db = streaming._build_session_db_for_stream(Path("/tmp/profile/state.db"))

    assert db is None


def test_self_heal_retry_reuses_request_state_db_path_for_new_agent():
    # Both retry branches should refresh session_db with _build_session_db_for_stream(...)
    # before creating the replacement agent.
    assert "_build_session_db_for_stream(_state_db_path)" in STREAMING_PY

    silent_block = STREAMING_PY[STREAMING_PY.index("# Rebuild agent kwargs and create a fresh agent"):]
    silent_branch_index = silent_block.index("agent = _AIAgent(**_agent_kwargs)")
    silent_assign = silent_block.index("_agent_kwargs['session_db'] = _build_session_db_for_stream(_state_db_path)")
    assert silent_assign < silent_branch_index

    exception_block = STREAMING_PY[STREAMING_PY.index("# Build a fresh agent with the new credentials"):]
    except_branch_index = exception_block.index("_heal_agent = _AIAgent(**_heal_kwargs)")
    except_assign = exception_block.index("_heal_kwargs['session_db'] = _build_session_db_for_stream(_state_db_path)")
    assert except_assign < except_branch_index
