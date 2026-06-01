from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
RUN_JOURNAL_PY = (ROOT / "api" / "run_journal.py").read_text(encoding="utf-8")

def test_stale_interrupted_event_marks_recovery_control():
    assert "\"recovery_control\": True" in RUN_JOURNAL_PY


def test_done_and_restore_filters_recovery_messages_from_frontend_state():
    assert "_filterRecoveryControlMessages(S.messages || [])" in MESSAGES_JS
    assert "if(!m||m.role==='tool') return false;" in MESSAGES_JS
    assert "if(m.recovery_control===true) return true;" in MESSAGES_JS
    assert "previous response was cut off by a network error" in MESSAGES_JS
    assert "continue exactly where you left off" in MESSAGES_JS


def test_apererror_recovers_on_recovery_control_event():
    assert "isRecoveryControlMessage=isInterrupted && (d.recovery_control===true || _streamRecoveryControlMessageText(d.message));" in MESSAGES_JS
    assert "Stream recovery signal received. Restoring transcript..." in MESSAGES_JS
    assert "if(await _restoreSettledSession(source)) return;" in MESSAGES_JS


def test_ui_rejects_recovery_control_as_visible_assistant_content():
    assert "function _isRecoveryControlMessageText" in UI_JS
    assert "function _assistantMessageHasVisibleContent" in UI_JS
    assert "if(_isRecoveryControlMessage(m)) return false;" in UI_JS
    assert "if(_isRecoveryControlMessage(m)){ri++;continue;}" in UI_JS
    assert "_assistantMessageHasVisibleContent(m)" in UI_JS


def test_recovery_control_detection_is_not_broad_phrase_matching():
    assert "|| /continue exactly where you left off/i.test(normalized)" not in UI_JS
    assert "|| /continue exactly where you left off/i.test(normalized)" not in MESSAGES_JS
    assert "const systemRecovery=/^\\[System:/i.test(normalized)" in UI_JS
    assert "const backendRecovery=/^the live worker stopped before this run finished\\.?$/i.test(normalized)" in UI_JS
