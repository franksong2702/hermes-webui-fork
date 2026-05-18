import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
NODE = shutil.which("node")


def test_new_session_coalesces_concurrent_create_requests_static_guard():
    """Regression for #2518: repeated clicks must not enqueue duplicate creates."""
    guard_pos = SESSIONS_JS.index("if(_newSessionPromise) return _newSessionPromise;")
    assign_pos = SESSIONS_JS.index("_newSessionPromise=(async()=>", guard_pos)
    api_pos = SESSIONS_JS.index("api('/api/session/new'", assign_pos)

    assert guard_pos < assign_pos < api_pos
    assert SESSIONS_JS.count("api('/api/session/new'") == 1
    assert "return _newSessionPromise;" in SESSIONS_JS[assign_pos:SESSIONS_JS.index("async function loadSession", assign_pos)]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_new_session_coalesces_concurrent_create_requests_in_runtime():
    """Execute sessions.js and verify two overlapping calls send one request."""
    script = textwrap.dedent(
        f"""
        const fs = require('fs');
        const vm = require('vm');
        const source = fs.readFileSync({json.dumps(str(ROOT / 'static' / 'sessions.js'))}, 'utf8');

        const calls = [];
        let resolveApi;
        const btn = {{
          disabled: false,
          attributes: {{}},
          classList: {{
            tokens: new Set(),
            toggle(name, active) {{
              if (active) this.tokens.add(name);
              else this.tokens.delete(name);
            }},
          }},
          setAttribute(name, value) {{ this.attributes[name] = value; }},
        }};
        const modelSelect = {{ value: 'gpt-test' }};
        const context = {{
          console,
          window: {{}},
          document: {{
            addEventListener() {{}},
            visibilityState: 'visible',
            hasFocus() {{ return true; }},
          }},
          localStorage: {{
            getItem() {{ return null; }},
            setItem() {{}},
            removeItem() {{}},
          }},
          S: {{
            activeProfile: 'default',
            activeStreamId: 'stream-1',
            busy: true,
            lastUsage: {{}},
            messages: [],
            session: null,
            toolCalls: [],
            _profileDefaultWorkspace: null,
            _profileSwitchWorkspace: null,
          }},
          $(id) {{
            if (id === 'btnNewChat') return btn;
            if (id === 'modelSelect') return modelSelect;
            return null;
          }},
          _applyModelToDropdown(model, select) {{ select.value = model; return true; }},
          _modelStateForSelect(_select, model) {{ return {{ model, model_provider: 'test-provider' }}; }},
          _setActiveSessionUrl() {{}},
          _syncCtxIndicator() {{}},
          clearLiveToolCards() {{}},
          loadDir() {{}},
          renderMessages() {{}},
          setComposerStatus() {{}},
          setStatus() {{}},
          syncModelChip() {{}},
          syncTopbar() {{}},
          updateQueueBadge() {{}},
          updateSendBtn() {{}},
          addEventListener() {{}},
          api(path, opts) {{
            if (path !== '/api/session/new') return Promise.resolve({{ ok: true }});
            calls.push({{ path, opts }});
            return new Promise((resolve) => {{
              resolveApi = () => resolve({{
                session: {{
                  session_id: 'sid-1',
                  messages: [],
                  last_usage: {{}},
                  model: 'gpt-test',
                  model_provider: 'test-provider',
                  message_count: 0,
                }},
              }});
            }});
          }},
        }};
        context.window = context;
        vm.createContext(context);
        vm.runInContext(source + '\\nglobalThis.__newSession = newSession;', context);

        const p1 = context.__newSession(true);
        const p2 = context.__newSession(true);
        if (calls.length !== 1) throw new Error(`expected one request while pending, got ${{calls.length}}`);
        if (!btn.disabled) throw new Error('new conversation button was not disabled while pending');
        if (btn.attributes['aria-busy'] !== 'true') throw new Error('aria-busy was not set while pending');
        if (!btn.classList.tokens.has('is-loading')) throw new Error('loading class was not set while pending');

        resolveApi();
        Promise.all([p1, p2]).then(() => {{
          if (calls.length !== 1) throw new Error(`expected one total request, got ${{calls.length}}`);
          if (btn.disabled) throw new Error('new conversation button stayed disabled after settle');
          if (btn.attributes['aria-busy'] !== 'false') throw new Error('aria-busy was not cleared after settle');
          if (btn.classList.tokens.has('is-loading')) throw new Error('loading class was not cleared after settle');
          process.stdout.write(JSON.stringify({{ calls: calls.length, disabled: btn.disabled }}));
        }}).catch((err) => {{
          console.error(err && err.stack || err);
          process.exit(1);
        }});
        """
    )
    assert NODE is not None
    proc = subprocess.run([NODE, "-e", script], text=True, capture_output=True, check=True)
    assert json.loads(proc.stdout) == {"calls": 1, "disabled": False}


def test_new_session_busy_state_clears_after_success_or_failure():
    """The sidebar + button should show busy while the create request is in flight."""
    helper_pos = SESSIONS_JS.index("function _setNewSessionCreating(active)")
    fn_pos = SESSIONS_JS.index("async function newSession")
    finally_pos = SESSIONS_JS.index("finally{", fn_pos)
    clear_pos = SESSIONS_JS.index("_newSessionPromise=null;", finally_pos)
    idle_pos = SESSIONS_JS.index("_setNewSessionCreating(false);", clear_pos)

    assert helper_pos < fn_pos
    assert "_setNewSessionCreating(true);" in SESSIONS_JS[fn_pos:finally_pos]
    assert finally_pos < clear_pos < idle_pos
    assert "btn.disabled=!!active" in SESSIONS_JS
    assert "btn.setAttribute('aria-busy',active?'true':'false')" in SESSIONS_JS


def test_new_session_button_has_disabled_feedback_style():
    assert ".panel-head-btn:disabled" in STYLE_CSS
    assert "cursor:wait" in STYLE_CSS
    assert ".panel-head-btn:disabled:hover" in STYLE_CSS
