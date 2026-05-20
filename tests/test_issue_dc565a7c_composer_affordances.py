"""Composer affordance regressions for kanban task t_dc565a7c.

These tests intentionally stay static: the task is scoped to copy, labels, and
layout containment, not runtime behavior changes.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
STYLE = ROOT / "static" / "style.css"
UI = ROOT / "static" / "ui.js"


class ElementCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        element_id = attr_map.get("id")
        if element_id:
            self.by_id[element_id] = {"tag": tag, **attr_map}


def _elements() -> dict[str, dict[str, str | None]]:
    parser = ElementCollector()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    return parser.by_id


def _css_rules(selector: str) -> list[str]:
    css = STYLE.read_text(encoding="utf-8")
    matches = re.findall(rf"{re.escape(selector)}\s*\{{([^}}]+)\}}", css)
    assert matches, f"missing CSS rule for {selector}"
    return matches


def _css_rule_with(selector: str, declaration: str) -> str:
    for rule in _css_rules(selector):
        if declaration in rule:
            return rule
    raise AssertionError(f"missing {declaration!r} in CSS rules for {selector}")


def test_composer_input_and_primary_action_have_stable_accessible_labels() -> None:
    elements = _elements()

    msg = elements["msg"]
    assert msg["tag"] == "textarea"
    assert msg["aria-label"] == "Message Hermes"
    assert msg["title"] == "Message Hermes"

    send = elements["btnSend"]
    assert send["tag"] == "button"
    assert send["type"] == "button"
    assert send["aria-label"] == "Type a message to send"
    assert send["data-tooltip"] == "Type a message to send"
    assert "title" not in send, "custom-tooltip buttons should not also emit native title tooltips"


def test_secondary_composer_controls_explain_their_action() -> None:
    elements = _elements()
    expected_labels = {
        "btnAttach": "Attach files",
        "btnMic": "Dictate message",
        "btnVoiceMode": "Start hands-free voice mode",
        "profileChip": "Switch active profile",
        "composerWorkspaceChip": "Switch workspace",
        "composerModelChip": "Switch conversation model",
        "composerReasoningChip": "Set reasoning effort level",
        "composerToolsetsChip": "Choose session toolsets",
    }

    for element_id, label in expected_labels.items():
        assert elements[element_id]["aria-label"] == label

    for element_id in [
        "profileChip",
        "composerWorkspaceChip",
        "composerModelChip",
        "composerReasoningChip",
        "composerToolsetsChip",
    ]:
        assert elements[element_id]["aria-haspopup"] == "listbox"
        assert elements[element_id]["aria-expanded"] == "false"


def test_composer_layout_uses_border_box_containment_to_prevent_laptop_overflow() -> None:
    for selector in [".messages-inner", ".composer-wrap", ".composer-box", "textarea#msg", ".composer-footer"]:
        _css_rule_with(selector, "box-sizing:border-box")

    composer_box = _css_rule_with(".composer-box", "width:100%")
    assert "max-width:780px" in composer_box

    composer_footer = _css_rule_with(".composer-footer", "width:100%")
    assert "container-name:composer-footer" in composer_footer


def test_send_button_tooltip_copy_updates_with_primary_action_state() -> None:
    ui = UI.read_text(encoding="utf-8")
    assert "_setButtonTooltip(btn,_btnTitle)" in ui
    assert "btn.setAttribute('aria-label',_btnTitle)" in ui
