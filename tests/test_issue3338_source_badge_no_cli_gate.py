"""
Regression coverage for issue #3338:
session source badges in the chat-pane topbar must not depend on `is_cli_session`.

Both the main chat topbar (`syncTopbar`) and titlebar (`syncAppTitlebar`) should
render the same source badge whenever source metadata exists.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name} not found"
    open_brace = src.find("{", start)
    assert open_brace != -1, f"{name} missing function body"
    depth = 0
    i = open_brace
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError(f"{name} body is unterminated")


def _normalize(src: str) -> str:
    return "".join(ch for ch in src if ch not in " \t\n\r")


def test_issue3338_topbar_syncTopbar_reads_source_metadata_without_cli_gate():
    sync_topbar = _extract_function(UI_JS, "syncTopbar")
    compact = _normalize(sync_topbar)

    assert "is_cli_session" not in compact, (
        "syncTopbar should not gate source badges behind is_cli_session; "
        "source metadata must be shown for messaging sessions too."
    )
    assert "S.session.source_label||S.session.source_tag||S.session.raw_source" in compact, (
        "syncTopbar must derive badge text from source_label/source_tag/raw_source"
    )


def test_issue3338_syncAppTitlebar_reads_source_metadata_without_cli_gate():
    sync_app_titlebar = _extract_function(PANELS_JS, "syncAppTitlebar")
    compact = _normalize(sync_app_titlebar)

    assert "S.session.is_cli_session" not in compact, (
        "syncAppTitlebar must not gate source badges behind is_cli_session"
    )
    assert "sourceLabel=S.session.source_label||S.session.source_tag||S.session.raw_source||''" in compact, (
        "syncAppTitlebar should assign sourceLabel from source metadata fallback chain"
    )
