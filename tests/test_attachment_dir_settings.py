"""Attachment-directory settings UI and config behavior."""

from pathlib import Path

import pytest


def test_attachment_root_uses_webui_config_relative_to_state_dir(tmp_path, monkeypatch):
    from api import config
    from api.upload import _attachment_root

    cfg = tmp_path / "config.yaml"
    cfg.write_text("webui:\n  attachment_dir: relative-inbox\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(cfg))
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)
    config.reload_config()

    assert _attachment_root() == (config.STATE_DIR / "relative-inbox").resolve()


def test_attachment_dir_env_override_wins_over_config(tmp_path, monkeypatch):
    from api import config
    from api.upload import _attachment_root

    cfg = tmp_path / "config.yaml"
    cfg.write_text("webui:\n  attachment_dir: config-inbox\n", encoding="utf-8")
    env_root = tmp_path / "env-inbox"
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(env_root))
    config.reload_config()

    status = config.get_attachment_dir_status()
    assert status["source"] == "env"
    assert status["editable"] is False
    assert _attachment_root() == env_root.resolve()


def test_setting_attachment_dir_rejects_relative_traversal(monkeypatch):
    from api import config

    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)
    with pytest.raises(ValueError, match="Relative attachment directories"):
        config.normalize_attachment_dir("../outside", create=False)


def test_setting_attachment_dir_writes_webui_config(tmp_path, monkeypatch):
    from api import config

    cfg = tmp_path / "config.yaml"
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(cfg))
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)
    config.reload_config()

    status = config.set_webui_attachment_dir(str(tmp_path / "uploads"))

    assert status["source"] == "config"
    assert status["configured_value"] == str(tmp_path / "uploads")
    assert "attachment_dir" in cfg.read_text(encoding="utf-8")


def test_attachment_dir_settings_ui_is_wired():
    repo = Path(__file__).resolve().parents[1]
    html = (repo / "static" / "index.html").read_text(encoding="utf-8")
    js = (repo / "static" / "panels.js").read_text(encoding="utf-8")

    assert 'id="settingsAttachmentDir"' in html
    assert 'id="settingsAttachmentDirStatus"' in html
    assert "_renderAttachmentDirSettings(settings.attachment_dir)" in js
    assert "body.attachment_dir=attachmentDirField.value||'';" in js
    assert "if(saved.attachment_dir) _renderAttachmentDirSettings(saved.attachment_dir);" in js
    assert "{input_value:attachmentDirField.value},e.message" in js
