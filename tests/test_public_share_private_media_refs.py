from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from api import shares
from api.models import Session

from tests.test_data_uri_images import _DRIVER_SRC

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "safe.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    return root


def _sanitize(text: str, *, workspace: Path | None = None) -> str:
    roots = (workspace,) if workspace is not None else ()
    out = shares._sanitize_message(
        {"role": "assistant", "content": text},
        allowed_roots=roots,
    )
    assert out is not None
    return out["content"]


@pytest.mark.parametrize(
    "text",
    [
        "file:///tmp/private.png",
        "[open](file:///tmp/private.png)",
        "`file:///tmp/private.png`",
        "MEDIA:https://webui.example/api/media?path=/tmp/private.png",
        "MEDIA:https://webui.example/API/MEDIA?PATH=/tmp/private.png",
        "MEDIA:https://cdn.example/render?next=https%3A%2F%2Fwebui.example%2Fapi%2Fmedia%3Fpath%3D%2Ftmp%2Fprivate.png",
        "MEDIA:https://cdn.example/render?next=https%253A%252F%252Fwebui.example%252Fapi%252Fmedia%253Fpath%253D%252Ftmp%252Fprivate.png",
        "MEDIA:https://cdn.example/render?next=file%3A%2F%2F%2Ftmp%2Fprivate.png",
        "`MEDIA:https://webui.example/api/media?path=/tmp/private.png`",
    ],
)
def test_public_share_snapshot_omits_private_renderer_media_references(text):
    content = _sanitize(text)

    assert content == shares._PLACEHOLDER
    assert "file://" not in content.lower()
    assert "/api/media" not in content.lower()


def test_public_https_media_without_private_endpoint_is_preserved():
    text = "MEDIA:https://cdn.example/images/public.png?size=large"

    assert _sanitize(text) == text


def test_external_api_media_like_path_without_path_parameter_is_preserved():
    text = "MEDIA:https://cdn.example/api/media/public-image.png"

    assert _sanitize(text) == text


def test_deep_dot_segments_cannot_evade_private_media_route():
    text = (
        "MEDIA:https://webui.example/api/1/2/3/4/5/6/7/8/9/"
        "../../../../../../../../../media?path=/tmp/private.png"
    )

    assert _sanitize(text) == shares._PLACEHOLDER


def test_direct_markdown_image_to_private_media_is_omitted():
    text = "![private](https://webui.example/api/media?path=/tmp/private.png)"

    assert _sanitize(text) == shares._PLACEHOLDER


def test_public_media_path_with_fragment_path_text_is_preserved():
    text = (
        "MEDIA:https://cdn.example/albums/api/media/photos/2024.jpg"
        "#path=screenshot.png"
    )

    assert _sanitize(text) == text


def test_public_markdown_image_with_api_media_path_segment_is_preserved():
    text = (
        "![public](https://cdn.example/albums/api/media/photos/2024.jpg"
        "#path=screenshot.png)"
    )

    assert _sanitize(text) == text


def test_existing_safe_local_image_embedding_remains_self_contained(workspace):
    content = _sanitize("MEDIA:safe.png", workspace=workspace)

    assert content.startswith('<img src="data:image/png;base64,')
    assert "api/media" not in content
    assert shares._PLACEHOLDER not in content

@pytest.mark.parametrize(
    "text",
    [
        "MEDIA:https://webui.example/api//media?path=/tmp/private.png",
        "MEDIA:https://webui.example/api/./media?path=/tmp/private.png",
        "MEDIA:https://webui.example/api/private/../media?path=/tmp/private.png",
        "MEDIA:https://webui.example/api/%70rivate/%2e%2e/media?%70ath=/tmp/private.png",
        "MEDIA:https://webui.example/api/media?path&#61;/tmp/private.png",
        "MEDIA:https://cdn.example/render?next=file%253A%252F%252F%252Ftmp%252Fprivate.png",
    ],
)
def test_public_share_private_media_normalization_fails_closed(text):
    assert _sanitize(text) == shares._PLACEHOLDER


def test_public_share_private_media_decode_depth_fails_closed():
    nested = "file:///tmp/private.png"
    for _ in range(shares._SHARE_MEDIA_SAFETY_DECODE_ROUNDS + 1):
        nested = nested.replace("%", "%25").replace(":", "%3A").replace("/", "%2F")
    text = f"MEDIA:https://cdn.example/render?next={nested}"

    assert _sanitize(text) == shares._PLACEHOLDER


def test_public_share_oversized_media_token_fails_closed():
    text = "MEDIA:https://cdn.example/" + ("a" * (shares._SHARE_MEDIA_SAFETY_MAX_CHARS + 1))

    assert _sanitize(text) == shares._PLACEHOLDER


def test_private_media_replacement_preserves_surrounding_public_text():
    content = _sanitize(
        "before MEDIA:https://webui.example/api/media?path=/tmp/private.png after"
    )

    assert content == f"before {shares._PLACEHOLDER} after"


def test_public_share_title_preserves_ordinary_public_media(tmp_path):
    public = "MEDIA:https://cdn.example/images/title.png"
    session = Session(
        session_id="share-public-media-title",
        title=public,
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )

    assert shares.build_share_snapshot(session)["title"] == public




@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_public_snapshot_stays_private_through_production_renderer(tmp_path):
    session = Session(
        session_id="share-renderer-private-media",
        title="Renderer closure",
        messages=[
            {
                "role": "assistant",
                "content": (
                    "before "
                    "MEDIA:https://webui.example/api/media?path=/tmp/private.png "
                    "and file:///tmp/private.pdf after"
                ),
            }
        ],
        workspace=str(tmp_path),
    )
    snapshot = shares.build_share_snapshot(session)
    content = snapshot["messages"][0]["content"]
    driver = tmp_path / "share-render-driver.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(driver), str(REPO_ROOT / "static" / "ui.js")],
        input=content,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    rendered = result.stdout.lower()
    assert "api/media?path=" not in rendered
    assert "file://" not in rendered
    assert shares._PLACEHOLDER.lower().strip("[]*") in rendered


def test_public_share_title_uses_same_private_media_boundary(tmp_path):
    session = Session(
        session_id="share-private-media-title",
        title="MEDIA:https://webui.example/api/media?path=/tmp/title.png",
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )

    snapshot = shares.build_share_snapshot(session)

    assert snapshot["title"] == shares._PLACEHOLDER
    assert "/api/media" not in snapshot["title"].lower()


def test_public_share_title_omits_file_uri(tmp_path):
    session = Session(
        session_id="share-private-file-title",
        title="file:///tmp/private-title.txt",
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )

    snapshot = shares.build_share_snapshot(session)

    assert snapshot["title"] == shares._PLACEHOLDER
    assert "file://" not in snapshot["title"].lower()
