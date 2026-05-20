"""Static regression coverage for visible session-control actions.

Issue shape: queue/interrupt/steer/new/stop existed only as slash commands or
implicit composer states, so users had to know hidden commands to control an
active session. The Conversation pane should expose those same actions as
visible buttons while still delegating to the slash-command handlers.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_conversation_pane_exposes_visible_session_control_actions():
    html = _read("index.html")

    assert 'id="sessionControlGrid"' in html
    for action in ("new", "stop", "queue", "interrupt", "steer"):
        assert f"runVisibleSessionControl('{action}')" in html
        assert f">/{action}</span>" in html
    assert 'id="btnClearConvModal"' in html
    assert '>/clear</span>' in html


def test_visible_actions_delegate_to_existing_slash_command_handlers():
    js = _read("commands.js")

    assert "async function runVisibleSessionControl(action)" in js
    assert "if(action==='new') await cmdNew();" in js
    assert "else if(action==='stop') await cmdStop();" in js
    assert "if(action==='queue') await cmdQueue(text);" in js
    assert "else if(action==='interrupt') await cmdInterrupt(text);" in js
    assert "else if(action==='steer') await cmdSteer(text);" in js
    assert "typeof clearConversation==='function'" in js


def test_visible_action_enabled_state_is_synchronized_from_session_state():
    js = _read("commands.js")
    ui = _read("ui.js")
    panels = _read("panels.js")

    assert "function syncVisibleSessionControls()" in js
    assert "stop:!hasSession||!hasActive" in js
    assert "queue:!hasSession||!isBusy||!draft" in js
    assert "interrupt:!hasSession||!hasActive||!draft" in js
    assert "steer:!hasSession||!hasActive||!draft" in js
    assert "clear:!hasSession||isBusy||!hasMessages" in js
    assert "syncVisibleSessionControls" in ui
    assert "syncVisibleSessionControls" in panels


def test_session_control_actions_have_distinct_styling_hooks():
    css = _read("style.css")

    assert ".session-control-grid" in css
    assert ".session-control-help" in css
    assert ".session-action-slash" in css
    assert '.settings-action-btn[aria-busy="true"]' in css
