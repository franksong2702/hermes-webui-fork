from pathlib import Path

import pytest

from api import config
from api.routes import _file_raw_target
from api.streaming import _build_native_multimodal_message
from api.upload import _session_attachment_dir


_MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0bIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _SessionStub:
    def __init__(self, workspace: Path):
        self.workspace = str(workspace)


def test_attachment_root_defaults_to_state_attachments(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)

    assert config.resolve_attachment_root({}) == (state_dir / "attachments").resolve()


def test_attachment_root_uses_relative_config_under_state_dir(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)

    root = config.resolve_attachment_root({"webui": {"attachment_dir": "uploads"}})

    assert root == (state_dir / "uploads").resolve()
    assert root.is_relative_to(state_dir.resolve())


def test_attachment_root_env_overrides_config(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    env_root = tmp_path / "env-attachments"
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(env_root))

    assert config.resolve_attachment_root({"webui": {"attachment_dir": "configured"}}) == env_root.resolve()


def test_attachment_root_accepts_absolute_config_path(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    absolute_root = tmp_path / "absolute-attachments"
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)

    assert config.resolve_attachment_root({"webui": {"attachment_dir": str(absolute_root)}}) == absolute_root.resolve()


@pytest.mark.parametrize("raw", ["../outside", "/"])
def test_attachment_root_rejects_unsafe_paths(monkeypatch, tmp_path, raw):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)

    with pytest.raises(ValueError, match="attachment directory"):
        config.resolve_attachment_root({"webui": {"attachment_dir": raw}})


def test_set_attachment_dir_persists_webui_key(monkeypatch, tmp_path):
    config_path = tmp_path / "hermes-home" / "config.yaml"
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)

    status = config.set_attachment_dir("configured-attachments")

    assert status["attachment_dir"] == str((tmp_path / "state" / "configured-attachments").resolve())
    assert "configured-attachments" in config_path.read_text(encoding="utf-8")


def test_upload_session_dir_uses_configured_attachment_root(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "cfg", {"webui": {"attachment_dir": "configured"}})
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)

    assert _session_attachment_dir("sess-1") == (state_dir / "configured" / "sess-1").resolve()


def test_file_raw_reads_session_scoped_legacy_attachment_dir(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    legacy_root = tmp_path / "legacy-attachments"
    session_dir = legacy_root / "sess-legacy"
    session_dir.mkdir(parents=True)
    attachment = session_dir / "photo.png"
    attachment.write_bytes(b"png")
    workspace.mkdir()

    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(
        config,
        "cfg",
        {"webui": {"attachment_dir": "new-attachments", "attachment_legacy_dirs": [str(legacy_root)]}},
    )
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_DIR", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_ATTACHMENT_LEGACY_DIRS", raising=False)

    assert _file_raw_target(_SessionStub(workspace), "sess-legacy", "photo.png") == attachment.resolve()


def test_native_image_reads_configured_session_attachment_dir(monkeypatch, tmp_path):
    attachment_root = tmp_path / "attachments"
    workspace = tmp_path / "workspace"
    session_dir = attachment_root / "sess-native"
    workspace.mkdir()
    session_dir.mkdir(parents=True)
    image = session_dir / "photo.png"
    image.write_bytes(_MINIMAL_PNG)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(attachment_root))

    result = _build_native_multimodal_message(
        "",
        "describe",
        [{"name": "photo.png", "path": str(image), "mime": "image/png", "session_id": "sess-native"}],
        str(workspace),
        session_id="sess-native",
    )

    assert isinstance(result, list)
    assert result[1]["type"] == "image_url"


def test_native_image_rejects_bare_attachment_root_file(monkeypatch, tmp_path):
    attachment_root = tmp_path / "attachments"
    workspace = tmp_path / "workspace"
    attachment_root.mkdir()
    workspace.mkdir()
    image = attachment_root / "not-session-scoped.png"
    image.write_bytes(_MINIMAL_PNG)
    monkeypatch.setenv("HERMES_WEBUI_ATTACHMENT_DIR", str(attachment_root))

    result = _build_native_multimodal_message(
        "",
        "describe",
        [{"name": "not-session-scoped.png", "path": str(image), "mime": "image/png"}],
        str(workspace),
    )

    assert isinstance(result, str)
