from __future__ import annotations

from pathlib import Path
import base64
import struct
import zlib
import shutil
import subprocess
from urllib.parse import quote_from_bytes

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


def test_lowercase_wrapped_media_token_stays_inert_code():
    text = "`media:https://webui.example/api/media?path=/tmp/private.png`"

    assert _sanitize(text) == text


def test_public_link_before_file_link_on_same_line_is_preserved():
    text = "see [public](https://cdn.example/a.png) and [x](file:///etc/passwd)"

    content = _sanitize(text)

    assert content == f"see [public](https://cdn.example/a.png) and {shares._PLACEHOLDER}"


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


@pytest.mark.parametrize("mime", ["png", "jpeg", "gif", "webp", "avif", "svg+xml"])
def test_large_self_contained_base64_image_survives_snapshot(mime):
    ref = f"data:image/{mime};base64," + base64.b64encode(b"image" * 4000).decode()
    text = f"![chart]({ref})"
    assert len(ref) > shares._SHARE_MEDIA_SAFETY_MAX_CHARS
    assert _sanitize(text) == text


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("encoding", ["base64", "percent", "percent-private-text"])
def test_large_valid_png_survives_snapshot_and_production_renderer(tmp_path, encoding):
    # A complete PNG with deterministic, poorly compressible RGB pixels.
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    pixels = bytes((i * 73 + i // 256) % 256 for i in range(128 * 128 * 3))
    scanlines = b"".join(b"\0" + pixels[i:i + 384] for i in range(0, len(pixels), 384))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 128, 128, 8, 2, 0, 0, 0))
    if encoding == "percent-private-text":
        # Text inside image bytes is inert data, not a renderer-active URL.
        png += chunk(b"tEXt", b"Comment\0https://webui.example/api/media?path=/tmp/private.png file:///tmp/private.png")
    png += chunk(b"IDAT", zlib.compress(scanlines, level=0)) + chunk(b"IEND", b"")
    if encoding == "base64":
        ref = "data:image/png;base64," + base64.b64encode(png).decode()
    else:
        ref = "data:image/png," + quote_from_bytes(png, safe="")
    text = f"![chart]({ref})"
    assert len(ref) > shares._SHARE_MEDIA_SAFETY_MAX_CHARS
    session = Session(session_id="share-large-png", messages=[{"role": "assistant", "content": text}])
    driver = tmp_path / "large-png-render.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")
    # Establish renderer support before exercising the public snapshot boundary.
    original_rendered = subprocess.run(
        [NODE, str(driver), str(REPO_ROOT / "static" / "ui.js")],
        input=text, capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    assert f'src="{ref}"' in original_rendered
    content = shares.build_share_snapshot(session)["messages"][0]["content"]
    assert content == text
    rendered = subprocess.run(
        [NODE, str(driver), str(REPO_ROOT / "static" / "ui.js")],
        input=content, capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    assert f'src="{ref}"' in rendered



@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("mime", ["png", "jpg", "jpeg", "gif", "webp", "avif", "PNG"])
def test_large_percent_raster_forms_survive_snapshot_and_renderer(tmp_path, mime):
    ref = f"data:image/{mime}," + "%89" * 6000
    text = f"![image]({ref})"
    assert len(ref) > shares._SHARE_MEDIA_SAFETY_MAX_CHARS
    assert _sanitize(text) == text
    driver = tmp_path / "percent-raster-render.js"
    driver.write_text(_DRIVER_SRC, encoding="utf-8")
    rendered = subprocess.run(
        [NODE, str(driver), str(REPO_ROOT / "static" / "ui.js")],
        input=text, capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    assert f'src="{ref}"' in rendered


@pytest.mark.parametrize("encoding", ["base64", "percent"])
@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["below-limit", "at-limit", "over-limit"])
def test_self_contained_image_uri_size_boundary(encoding, offset):
    prefix = "data:image/png;base64," if encoding == "base64" else "data:image/png,"
    size = shares._SHARE_DATA_IMAGE_MAX_CHARS + offset
    payload = "A" * (size - len(prefix))
    if encoding == "percent":
        payload = "%89" + payload[3:]
    text = f"![image]({prefix}{payload})"
    assert _sanitize(text) == (text if offset <= 0 else shares._PLACEHOLDER)


@pytest.mark.parametrize("ref", [
    "data:image/svg+xml," + "%3Csvg%3E" * 3000,
    "data:image/png;charset=utf-8," + "%89" * 6000,
    "data:image/bmp," + "%89" * 6000,
    "data:text/html," + "%3Cscript%3E" * 2000,
    "data:image/png," + "%89" * 6000 + "?next=https://webui.example/api/media?path=private.png",
    "data:image/png," + "%89" * 6000 + "#fragment",
    "data:image/png," + "%89" * 6000 + "\\private.png",
    "data:image/png," + "%89" * 6000 + '<script>',
], ids=["percent-svg", "charset-parameter", "unsupported-raster", "html-scheme",
        "private-url-suffix", "fragment", "backslash", "html-payload"])
def test_large_non_renderer_percent_image_fails_closed(ref):
    assert _sanitize(f"![unsafe]({ref})") == shares._PLACEHOLDER


def test_percent_image_does_not_exempt_neighboring_private_references():
    image = "![image](data:image/png," + "%89" * 6000 + ")"
    text = (
        f"before {image} "
        "![private](https://webui.example/api/media?path=/tmp/private.png) "
        "file:///tmp/private.png after"
    )
    assert _sanitize(text) == f"before {image} {shares._PLACEHOLDER} {shares._PLACEHOLDER} after"


@pytest.mark.parametrize("ref", [
    "data:image/png;base64," + "A" * (2 * 1024 * 1024),
    "data:image/png;base64," + "A" * 17000 + "%2Fapi%2Fmedia%3Fpath%3Dprivate.png",
    "data:image/png;base64," + "A" * 17000 + "file:///tmp/private.png",
    "data:text/html;base64," + "A" * 17000,
], ids=["oversized", "encoded-private-path", "literal-file-path", "html-scheme"])
def test_large_non_renderer_base64_image_does_not_bypass_private_boundary(ref):
    assert _sanitize(f"![unsafe]({ref})") == shares._PLACEHOLDER


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


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "wrapped"])
@pytest.mark.parametrize("ref,private", [
    ("https://cdn.example/icon.png", False),
    ("https://cdn.example/albums/api/media/photos.png#path=public.png", False),
    ("/home/me/secret.png", True),
    ("relative-secret.png", True),
    ("file:///home/me/secret.png", True),
    ("https://webui.example/api/media?path=/home/me/secret.png", True),
    ("https://cdn.example/render?next=https%253A%252F%252Fwebui.example%252Fapi%252Fmedia%253Fpath%253Dsecret.png", True),
], ids=["public", "public-path-lookalike", "local-absolute", "local-relative",
        "file-uri", "private-endpoint", "encoded-private-endpoint"])
def test_public_share_title_wrapped_media_matrix(tmp_path, wrapped, ref, private):
    token = f"MEDIA:{ref}"
    if wrapped:
        token = f"`{token}`"
    text = f"See {token} here"
    session = Session(
        session_id="share-title-wrapped-media",
        title=text,
        messages=[{"role": "assistant", "content": text}],
        workspace=str(tmp_path),
    )
    snapshot = shares.build_share_snapshot(session)
    expected_title = f"See {shares._PLACEHOLDER} here" if private else text
    assert snapshot["title"] == expected_title
    # Bodies retain the production renderer's activation of wrapped MEDIA.
    if not private:
        assert snapshot["messages"][0]["content"] == f"See MEDIA:{ref} here"
    else:
        assert ref not in snapshot["messages"][0]["content"]


def test_public_share_title_wrapped_media_keeps_neighbors_and_lowercase(tmp_path):
    public = "`MEDIA:https://cdn.example/icon.png`"
    private = "`MEDIA:/home/me/secret.png`"
    lowercase = "`media:https://cdn.example/inert.png`"
    text = f"Reference {public} and {private} then {lowercase}"
    session = Session(
        session_id="share-title-wrapped-neighbors",
        title=text,
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )
    snapshot = shares.build_share_snapshot(session)
    assert snapshot["title"] == f"Reference {public} and {shares._PLACEHOLDER} then {lowercase}"


def test_public_share_title_preserves_exact_review_public_wrapper(tmp_path):
    text = "Reference `MEDIA:https://cdn.example/icon.png`"
    session = Session(
        session_id="share-title-exact-review",
        title=text,
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )
    assert shares.build_share_snapshot(session)["title"] == text


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "wrapped"])
def test_public_share_title_keeps_review_gif(tmp_path, wrapped):
    # Complete 1x1 GIF89a, matching the reviewer-pinned title shape.
    gif_uri = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    gif = base64.b64decode(gif_uri.split(",", 1)[1], validate=True)
    assert gif.startswith(b"GIF89a\x01\x00\x01\x00") and gif.endswith(b";")
    token = f"MEDIA:{gif_uri}"
    if wrapped:
        token = f"`{token}`"
    title = f"Logo {token}"
    session = Session(
        session_id="share-title-review-gif",
        title=title,
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )
    assert shares.build_share_snapshot(session)["title"] == title
    assert session.title == title


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "wrapped"])
@pytest.mark.parametrize("mime,encoding", [
    *((mime, "base64") for mime in ["png", "jpg", "jpeg", "gif", "webp", "avif", "svg+xml"]),
    *((mime, "percent") for mime in ["png", "jpg", "jpeg", "gif", "webp", "avif"]),
])
def test_public_share_title_keeps_supported_data_image_forms(tmp_path, wrapped, mime, encoding):
    # This matrix checks URI policy; the review GIF above checks a real image.
    suffix = ";base64," + base64.b64encode(b"image" * 4000).decode()
    if encoding == "percent":
        suffix = "," + "%89" * 6000
    ref = f"data:image/{mime}{suffix}"
    assert len(ref) > shares._SHARE_MEDIA_SAFETY_MAX_CHARS
    token = f"MEDIA:{ref}"
    if wrapped:
        token = f"`{token}`"
    title = f"Logo {token} here"
    session = Session(
        session_id="share-title-data-image-forms",
        title=title,
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )
    assert shares.build_share_snapshot(session)["title"] == title


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "wrapped"])
@pytest.mark.parametrize("encoding", ["base64", "percent"])
@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["below-limit", "at-limit", "over-limit"])
def test_public_share_title_data_image_size_boundary(tmp_path, wrapped, encoding, offset):
    prefix = "data:image/png;base64," if encoding == "base64" else "data:image/png,"
    payload = "A" * (shares._SHARE_DATA_IMAGE_MAX_CHARS + offset - len(prefix))
    if encoding == "percent":
        payload = "%89" + payload[3:]
    token = f"MEDIA:{prefix}{payload}"
    if wrapped:
        token = f"`{token}`"
    title = f"Logo {token} here"
    session = Session(
        session_id="share-title-image-size-boundary",
        title=title,
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )
    expected = title if offset <= 0 else f"Logo {shares._PLACEHOLDER} here"
    assert shares.build_share_snapshot(session)["title"] == expected


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "wrapped"])
@pytest.mark.parametrize("ref", [
    "data:image/svg+xml,%3Csvg%3E",
    "data:image/png;charset=utf-8,%89PNG",
    "data:image/bmp;base64,AAAA",
    "data:text/html;base64,AAAA",
    "data:image/png;base64,AAAA%2Fapi%2Fmedia%3Fpath%3Dprivate.png",
    "data:image/png;base64,AAAA" + "file:///tmp/private.png",
    "data:image/png;base64," + "A" * 17000 + "%2Fapi%2Fmedia%3Fpath%3Dprivate.png",
    "data:image/png," + "%89" * 6000 + "?next=https://webui.example/api/media?path=private.png",
], ids=["percent-svg", "charset-parameter", "unsupported-raster", "html-scheme",
        "encoded-private-base64-suffix", "literal-file-base64-suffix",
        "large-malformed-base64", "private-url-percent-suffix"])
def test_public_share_title_rejects_unsupported_data_image_forms(tmp_path, wrapped, ref):
    token = f"MEDIA:{ref}"
    if wrapped:
        token = f"`{token}`"
    session = Session(
        session_id="share-title-data-image-negative",
        title=f"Logo {token} here",
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )
    assert shares.build_share_snapshot(session)["title"] == f"Logo {shares._PLACEHOLDER} here"


def test_public_share_title_image_exemption_does_not_exempt_private_neighbors(tmp_path):
    ref = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    image = f"`MEDIA:{ref}`"
    title = (
        f"Logo {image} and `MEDIA:/home/me/secret.png` then "
        "MEDIA:https://cdn.example/render?next=https%253A%252F%252Fwebui.example%252Fapi%252Fmedia%253Fpath%253Dsecret.png"
    )
    session = Session(
        session_id="share-title-data-image-neighbors",
        title=title,
        messages=[{"role": "user", "content": "hello"}],
        workspace=str(tmp_path),
    )
    expected = f"Logo {image} and {shares._PLACEHOLDER} then {shares._PLACEHOLDER}"
    assert shares.build_share_snapshot(session)["title"] == expected
