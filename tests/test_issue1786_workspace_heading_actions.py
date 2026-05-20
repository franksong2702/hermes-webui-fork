from pathlib import Path
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).parent))
from conftest import TEST_BASE, TEST_WORKSPACE


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
WORKSPACE_JS = (ROOT / "static" / "workspace.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def test_workspace_heading_is_interactive_root_control():
    """The WORKSPACE panel heading should behave like the breadcrumb root."""
    assert 'id="workspacePanelHeading"' in INDEX_HTML
    assert "bindWorkspaceHeadingActions" in UI_JS
    assert "loadDir('.')" in UI_JS


def test_workspace_heading_context_menu_exposes_root_reveal_and_copy_path():
    """Right-clicking the heading should expose root-scoped Reveal and Copy path actions."""
    assert "_showWorkspaceRootContextMenu" in UI_JS
    assert "'/api/file/reveal'" in UI_JS
    assert "'/api/file/path'" in UI_JS
    assert "path:'.'" in UI_JS.replace(" ", "")
    assert "copy_file_path" in UI_JS
    assert "reveal_in_finder" in UI_JS


def test_workspace_heading_affordance_requires_workspace():
    """The heading should only advertise button behavior when a workspace exists."""
    heading_line = next(line for line in INDEX_HTML.splitlines() if 'id="workspacePanelHeading"' in line)
    assert 'role="button"' not in heading_line
    assert 'tabindex="0"' not in heading_line
    assert "_syncWorkspaceHeadingState" in UI_JS
    assert "heading.classList.toggle('workspace-panel-heading--enabled',enabled)" in UI_JS
    assert "heading.setAttribute('role','button')" in UI_JS
    assert "heading.setAttribute('tabindex','0')" in UI_JS
    assert "heading.removeAttribute('role')" in UI_JS
    assert "heading.removeAttribute('tabindex')" in UI_JS
    assert "const enabled=!!_activeWorkspaceRootPayload();" in UI_JS
    assert "typeof _syncWorkspaceHeadingState==='function'" in UI_JS

    context_idx = UI_JS.find("heading.oncontextmenu")
    guard_idx = UI_JS.find("if(!payload) return;", context_idx)
    prevent_idx = UI_JS.find("e.preventDefault()", context_idx)
    assert context_idx < guard_idx < prevent_idx


def test_workspace_heading_uses_profile_default_workspace_without_session():
    """Blank-state headings should work from the displayed default workspace."""
    assert "function _activeWorkspaceRootPayload()" in UI_JS
    assert "S._profileDefaultWorkspace" in UI_JS
    assert "const payload=_activeWorkspaceRootPayload();" in UI_JS
    assert "if(payload.session_id) loadDir('.');" in UI_JS
    assert "else loadDir('.',payload);" in UI_JS


def test_root_context_menu_posts_workspace_payload_not_session_only():
    """Root reveal/copy actions should not require S.session.session_id."""
    idx = UI_JS.index("function _showWorkspaceRootContextMenu")
    block = UI_JS[idx:idx + 1400]
    assert "const payload=_activeWorkspaceRootPayload();" in block
    assert "if(!payload) return;" in block
    assert "JSON.stringify({...payload,path:'.'})" in block


def test_load_dir_accepts_default_workspace_payload():
    """loadDir should list a default workspace root before any session exists."""
    assert "async function loadDir(path, workspacePayload)" in WORKSPACE_JS
    assert "const payload=workspacePayload||((typeof _activeWorkspaceRootPayload==='function')?_activeWorkspaceRootPayload():null);" in WORKSPACE_JS
    assert "params.set('workspace',payload.workspace);" in WORKSPACE_JS


def test_backend_accepts_workspace_fallback_for_root_actions():
    """Backend root path/reveal/list calls must support the pre-session default workspace."""
    assert "def _workspace_root_from_session_or_body(body):" in ROUTES_PY
    assert "resolve_trusted_workspace(body.get(\"workspace\"))" in ROUTES_PY
    assert "_workspace_root_from_session_or_body(body)" in ROUTES_PY

    idx = ROUTES_PY.index("def _handle_list_dir")
    block = ROUTES_PY[idx:idx + 1500]
    assert "workspace_arg = qs.get(\"workspace\", [\"\"])[0]" in block
    assert "workspace = str(resolve_trusted_workspace(workspace_arg))" in block


def _post(path, body):
    req = urllib.request.Request(
        TEST_BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


def test_file_path_accepts_workspace_without_session():
    body, status = _post(
        "/api/file/path",
        {"workspace": str(TEST_WORKSPACE), "path": "missing-ok-from-root.txt"},
    )
    assert status == 200, body
    assert body.get("ok") is True
    assert body.get("path", "").endswith("missing-ok-from-root.txt")


def test_list_dir_accepts_workspace_without_session():
    qs = urllib.parse.urlencode({"workspace": str(TEST_WORKSPACE), "path": "."})
    with urllib.request.urlopen(TEST_BASE + "/api/list?" + qs, timeout=10) as r:
        body = json.loads(r.read())
        status = r.status
    assert status == 200, body
    assert isinstance(body.get("entries"), list)


def test_invalid_workspace_fallback_returns_json_error_not_crash():
    body, status = _post(
        "/api/file/path",
        {"workspace": "/definitely/not/a/workspace/root", "path": "."},
    )
    assert status == 400, body
    assert "error" in body
