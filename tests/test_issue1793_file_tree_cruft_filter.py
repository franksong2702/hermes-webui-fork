"""Regression coverage for #1793 — workspace file-tree cruft filter.

Original v0.51.21 work added an inline "Show hidden files" toggle that sat
permanently between the breadcrumb and the file tree, eating ~32px of
vertical space on every panel view (root, subdir, file preview).

Follow-up UX refinements moved the toggle behind a kebab dropdown in the
panel-actions row and surface the non-default "hidden-files-visible" state via
a small indicator next to the panel heading. Later follow-up coverage keeps the
same render-boundary filtering model while broadening the matcher so true
dotfiles / dot-directories are hidden by default until the user enables the
toggle.
"""

import json
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def _workspace_tree_behavior(js_body: str) -> dict:
    """Run the real workspace-tree filter/render helpers in a tiny DOM.

    The assertions below exercise observable behavior (which file names render
    and how the toggle round-trips), instead of pinning exact source strings.
    """
    start = UI_JS.index("const WORKSPACE_HIDDEN_FILE_NAMES")
    render_start = UI_JS.index("function _renderTreeItems", start)
    end = UI_JS.index("\n}\n\nasync function deleteWorkspaceDir", render_start) + 3
    snippet = UI_JS[start:end]
    script = textwrap.dedent(
        f"""
        const vm = require('vm');
        const storage = {{}};
        function makeEl(tag) {{
          return {{
            tagName: tag,
            children: [],
            style: {{}},
            attrs: {{}},
            className: '',
            textContent: '',
            _innerHTML: '',
            get innerHTML() {{ return this._innerHTML; }},
            set innerHTML(value) {{ this._innerHTML = String(value); this.children = []; }},
            hidden: false,
            appendChild(child) {{ this.children.push(child); return child; }},
            setAttribute(name, value) {{ this.attrs[name] = String(value); }},
            removeAttribute(name) {{ delete this.attrs[name]; }},
            replaceWith() {{}},
          }};
        }}
        function collectFileNames(node) {{
          const out = [];
          function walk(el) {{
            if (!el) return;
            if (el.className === 'file-name') out.push(el.textContent);
            for (const child of (el.children || [])) walk(child);
          }}
          walk(node);
          return out;
        }}
        const elements = {{ fileTree: makeEl('div'), wsEmptyState: makeEl('div') }};
        const ctx = {{
          S: {{
            showHiddenWorkspaceFiles: false,
            currentDir: '.',
            entries: [],
            session: {{ session_id: 'sid', workspace: '/tmp/workspace' }},
            _expandedDirs: new Set(),
            _dirCache: {{}},
          }},
          localStorage: {{
            getItem: (k) => Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null,
            setItem: (k, v) => {{ storage[k] = String(v); }},
          }},
          document: {{
            createElement: makeEl,
            body: makeEl('body'),
            addEventListener() {{}},
            removeEventListener() {{}},
          }},
          window: {{ addEventListener() {{}}, removeEventListener() {{}}, innerWidth: 1024, innerHeight: 768 }},
          setTimeout: (fn) => {{ if (typeof fn === 'function') fn(); return 0; }},
          $: (id) => elements[id] || null,
          t: (key) => key,
          fileIcon: () => '',
          loadDir: () => {{}},
          openFile: () => {{}},
          deleteWorkspaceFile: () => {{}},
          deleteWorkspaceDir: () => {{}},
          _saveExpandedDirs: () => {{}},
          api: () => Promise.resolve({{ entries: [] }}),
          showToast: () => {{}},
          showConfirmDialog: () => Promise.resolve(false),
          showPromptDialog: () => Promise.resolve(null),
          __storage: storage,
          __elements: elements,
          __makeEl: makeEl,
          __collectFileNames: collectFileNames,
        }};
        vm.createContext(ctx);
        vm.runInContext({json.dumps(snippet)}, ctx);
        const result = vm.runInContext({json.dumps('(() => { ' + js_body + ' })()')}, ctx);
        process.stdout.write(JSON.stringify(result));
        """
    )
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


# ── Original filtering behavior (must stay green) ────────────────────────


def test_workspace_panel_has_show_hidden_files_toggle():
    """File-tree cruft must be recoverable via an explicit user toggle.

    The toggle now lives behind the kebab; the checkbox itself is built by
    `_buildWorkspacePrefsMenu` in ui.js (so it's literally referenced there
    by id), but the existing call site in i18n still resolves the localized
    label.
    """
    assert "toggleWorkspaceHiddenFiles" in UI_JS
    assert 'id="workspaceShowHiddenFiles"' in UI_JS  # built dynamically; id preserved
    assert "workspace_show_hidden_files" in I18N_JS


def test_file_tree_filters_common_cruft_by_default():
    """macOS/Windows/VCS/cache noise should not render by default."""
    assert "WORKSPACE_HIDDEN_FILE_NAMES" in UI_JS
    for name in ["thumbs.db", "desktop.ini", "__pycache__", "node_modules"]:
        assert name in UI_JS
    assert "_visibleWorkspaceEntries" in UI_JS
    assert "S.showHiddenWorkspaceFiles" in UI_JS
    assert "_workspaceShouldHideEntry" in UI_JS


def test_file_tree_hides_dotfiles_and_hidden_dirs_by_default():
    """The hidden-files toggle must cover ordinary dotfiles/dot-directories,
    not only a curated cruft list like .DS_Store and .git.
    """
    assert "function _workspaceShouldHideName" in UI_JS
    body_start = UI_JS.index("function _workspaceShouldHideName")
    body_end = UI_JS.index("\n}", body_start)
    body = UI_JS[body_start:body_end]
    assert "raw.startsWith('.')" in body
    assert "WORKSPACE_HIDDEN_FILE_NAMES.has(lower)" in body
    assert "WORKSPACE_HIDDEN_FILE_SUFFIXES.some" in body
    for name in ["dist", "build", "node_modules", "venv"]:
        assert f"'{name}'" in UI_JS
    for suffix in [".pyc", ".pyo"]:
        assert f"'{suffix}'" in UI_JS


def test_show_hidden_toggle_bypasses_all_workspace_filtering():
    """When the toggle is on, dotfiles, generated dirs, and suffix matches
    must all be recoverable from the cached listing without a refetch.
    """
    entry_start = UI_JS.index("function _workspaceShouldHideEntry")
    entry_end = UI_JS.index("\n}", entry_start)
    entry_body = UI_JS[entry_start:entry_end]
    assert "S.showHiddenWorkspaceFiles" in entry_body
    assert "return _workspaceShouldHideName(item.name)" in entry_body

    visible_start = UI_JS.index("function _visibleWorkspaceEntries")
    visible_end = UI_JS.index("\n}", visible_start)
    visible_body = UI_JS[visible_start:visible_end]
    assert "S.showHiddenWorkspaceFiles?list:list.filter" in visible_body


def test_hidden_file_toggle_invalidates_tree_render_without_refetch():
    """The toggle should re-render cached entries instead of changing workspace state."""
    assert "function toggleWorkspaceHiddenFiles" in UI_JS
    body_start = UI_JS.index("function toggleWorkspaceHiddenFiles")
    body_end = UI_JS.index("\n}", body_start)
    body = UI_JS[body_start:body_end]
    assert "renderFileTree()" in body
    assert "localStorage.setItem('hermes-workspace-show-hidden-files'" in body


def test_workspace_file_tree_hides_dotfiles_by_default_behaviorally():
    result = _workspace_tree_behavior(
        """
        S.entries = [
          { name: 'README.md', type: 'file', path: 'README.md' },
          { name: '.env', type: 'file', path: '.env' },
          { name: '.config', type: 'dir', path: '.config' },
          { name: 'src', type: 'dir', path: 'src' },
          { name: 'node_modules', type: 'dir', path: 'node_modules' },
          { name: 'cache.pyc', type: 'file', path: 'cache.pyc' },
          { name: 'build', type: 'dir', path: 'build' },
        ];
        renderFileTree();
        return { rendered: __collectFileNames(__elements.fileTree) };
        """
    )
    assert result["rendered"] == ["README.md", "src"]


def test_workspace_show_hidden_toggle_round_trips_cached_listing_behaviorally():
    result = _workspace_tree_behavior(
        """
        S.entries = [
          { name: 'app.py', type: 'file', path: 'app.py' },
          { name: '.env', type: 'file', path: '.env' },
          { name: '__pycache__', type: 'dir', path: '__pycache__' },
        ];
        renderFileTree();
        const initial = __collectFileNames(__elements.fileTree);

        let renderCount = 0;
        const originalRender = renderFileTree;
        renderFileTree = () => {
          renderCount += 1;
          __elements.fileTree = __makeEl('div');
          originalRender();
        };

        toggleWorkspaceHiddenFiles(true);
        const enabled = __collectFileNames(__elements.fileTree);
        const storedEnabled = __storage['hermes-workspace-show-hidden-files'];

        toggleWorkspaceHiddenFiles(false);
        const disabledAgain = __collectFileNames(__elements.fileTree);
        const storedDisabled = __storage['hermes-workspace-show-hidden-files'];

        return { initial, enabled, disabledAgain, storedEnabled, storedDisabled, renderCount };
        """
    )
    assert result == {
        "initial": ["app.py"],
        "enabled": ["app.py", ".env", "__pycache__"],
        "disabledAgain": ["app.py"],
        "storedEnabled": "1",
        "storedDisabled": "0",
        "renderCount": 2,
    }


def test_workspace_hidden_filter_applies_to_expanded_nested_cached_dirs_behaviorally():
    result = _workspace_tree_behavior(
        """
        S.entries = [
          { name: 'src', type: 'dir', path: 'src' },
          { name: '.root-secret', type: 'file', path: '.root-secret' },
        ];
        S._expandedDirs = new Set(['src']);
        S._dirCache = {
          '.': S.entries,
          'src': [
            { name: 'app.py', type: 'file', path: 'src/app.py' },
            { name: '.nested-env', type: 'file', path: 'src/.nested-env' },
            { name: '.hidden-dir', type: 'dir', path: 'src/.hidden-dir' },
            { name: 'module.pyc', type: 'file', path: 'src/module.pyc' },
            { name: 'package', type: 'dir', path: 'src/package' },
          ],
        };
        renderFileTree();
        const hiddenOff = __collectFileNames(__elements.fileTree);
        toggleWorkspaceHiddenFiles(true);
        const hiddenOn = __collectFileNames(__elements.fileTree);
        return { hiddenOff, hiddenOn };
        """
    )
    assert result["hiddenOff"] == ["src", "app.py", "package"]
    assert result["hiddenOn"] == [
        "src",
        "app.py",
        ".nested-env",
        ".hidden-dir",
        "module.pyc",
        "package",
        ".root-secret",
    ]


# ── Kebab-affordance UX refinement ───────────────────────────────────────


def test_no_inline_workspace_hidden_toggle_row():
    """The always-on inline `<label class="workspace-hidden-toggle">` row
    must be gone — it ate vertical space below the breadcrumb on every
    panel view. Toggle now lives behind the kebab.
    """
    assert "workspace-hidden-toggle" not in INDEX_HTML, (
        "inline hidden-files row should have been removed in favor of the "
        "kebab menu (#1793 follow-up)"
    )
    # CSS for the inline row should also be gone — leaving stale rules
    # invites future drift where someone re-adds the row and it picks up
    # accidental styling.
    assert ".workspace-hidden-toggle" not in STYLE_CSS


def test_panel_actions_row_has_workspace_prefs_kebab():
    """A kebab button (`btnWorkspacePrefs`) must exist in the workspace
    panel actions row to expose the menu.
    """
    assert 'id="btnWorkspacePrefs"' in INDEX_HTML
    assert 'onclick="toggleWorkspacePrefsMenu(event)"' in INDEX_HTML
    # Tooltip is i18n-aware
    assert 'data-i18n-title="workspace_options"' in INDEX_HTML
    # Kebab carries an accent dot for non-default state
    assert 'id="workspacePrefsDot"' in INDEX_HTML


def test_panel_heading_has_hidden_files_indicator():
    """The non-default "hidden files visible" state must surface as a small
    indicator next to the WORKSPACE heading so users don't forget they
    flipped the pref. Hidden by default via the `hidden` attribute.
    """
    assert 'id="workspaceHiddenIndicator"' in INDEX_HTML
    # The indicator opens the same menu when clicked (no separate code path)
    block = INDEX_HTML[INDEX_HTML.index('id="workspaceHiddenIndicator"'):]
    block = block[: block.index("</span>") + 7]
    assert "toggleWorkspacePrefsMenu" in block
    # Default-hidden so the chip doesn't clutter normal state
    assert " hidden " in block or block.rstrip().endswith("hidden")


def test_kebab_menu_javascript_exists():
    """The dropdown must be self-contained: open/close/position handlers
    follow the canonical floating-menu pattern from
    `_openSessionActionMenu`.
    """
    assert "function toggleWorkspacePrefsMenu" in UI_JS
    assert "function _buildWorkspacePrefsMenu" in UI_JS
    assert "function _closeWorkspacePrefsMenu" in UI_JS
    assert "function _positionWorkspacePrefsMenu" in UI_JS
    # Built menu still contains the canonical input id so existing call
    # sites and the toggle test above keep working.
    build_start = UI_JS.index("function _buildWorkspacePrefsMenu")
    build_end = UI_JS.index("\n}", build_start)
    build_body = UI_JS[build_start:build_end]
    assert 'id="workspaceShowHiddenFiles"' in build_body


def test_kebab_menu_closes_on_escape_and_outside_click():
    """Standard keyboard / click-out close behavior."""
    # Escape closes
    assert "Escape" in UI_JS and "_closeWorkspacePrefsMenu" in UI_JS
    # Outside-click close listener
    assert "_workspacePrefsMenu" in UI_JS
    assert "if(_workspacePrefsMenu.contains(e.target)) return" in UI_JS


def test_indicator_reflects_localStorage_state_on_load():
    """`_syncWorkspaceHiddenToggle` must drive both the dropdown checkbox
    AND the indicator/dot so a page reload with the pref ON shows the
    "hidden visible" indicator without the user having to open the menu.
    """
    sync_start = UI_JS.index("function _syncWorkspaceHiddenToggle")
    sync_end = UI_JS.index("\n}", sync_start)
    body = UI_JS[sync_start:sync_end]
    assert "workspaceHiddenIndicator" in body
    assert "workspacePrefsDot" in body
    # Drives the existing checkbox if it's mounted
    assert "workspaceShowHiddenFiles" in body


def test_kebab_menu_styles_replace_inline_row():
    """CSS must define the kebab dot, indicator, and floating menu — but
    not the legacy inline-row styling (the test above pins removal).
    """
    assert ".workspace-prefs-menu{" in STYLE_CSS
    assert ".workspace-prefs-item{" in STYLE_CSS
    assert ".workspace-hidden-indicator{" in STYLE_CSS
    assert "#btnWorkspacePrefs" in STYLE_CSS


def test_new_i18n_keys_present_in_all_locales():
    """The new copy must exist in every locale block so the kebab menu
    description and indicator chip don't render `undefined` in non-en
    sessions.
    """
    # Total locale blocks today: 9 (en, ja, ru, es, de, zh, zh-Hant, pt, ko)
    n_locales = I18N_JS.count("workspace_show_hidden_files:")
    assert n_locales >= 8, f"unexpected locale count: {n_locales}"
    for key in (
        "workspace_show_hidden_files_desc:",
        "workspace_hidden_files_visible:",
        "workspace_hidden_files_visible_title:",
        "workspace_options:",
    ):
        assert I18N_JS.count(key) == n_locales, (
            f"key {key!r} missing in some locales (expected {n_locales}, "
            f"got {I18N_JS.count(key)})"
        )


# ── #1841 regression: exact non-English translations must be present ─────


def test_workspace_show_hidden_files_translations_are_not_english_fallback():
    """Each non-English locale must carry its own translated string for
    workspace_show_hidden_files — not silently fall back to the English
    "Show hidden files".  Pin the exact expected translations so a
    regression that replaces any of them with the English fallback is
    caught immediately.
    """
    expected = {
        "es": "Mostrar archivos ocultos",
        "ru": "Показывать скрытые файлы",
        "zh": "显示隐藏文件",
        "zh-Hant": "顯示隱藏檔案",
        "pt": "Mostrar arquivos ocultos",
        "ja": "隠しファイルを表示",
        "ko": "숨김 파일 표시",
    }
    for locale, translation in expected.items():
        # Build a source-level needle: the locale block assigns the
        # translated value on a line like
        #   workspace_show_hidden_files: 'Mostrar archivos ocultos',
        # Matching the full assignment avoids false positives from
        # unrelated strings that happen to contain the same words.
        needle = f"workspace_show_hidden_files: '{translation}'"
        assert needle in I18N_JS, (
            f"locale {locale!r}: expected translation needle {needle!r} "
            f"not found in i18n.js — likely fell back to English"
        )
