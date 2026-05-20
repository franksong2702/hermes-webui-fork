"""Desktop pet routes and support helpers.

This module intentionally owns the optional desktop pet surface so the main
WebUI router only needs thin dispatch hooks.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import threading
import time
import uuid
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

from api.agent_sessions import MESSAGING_SOURCES, is_cli_session_row, is_cli_session_row_visible
from api.config import STREAMS, STREAMS_LOCK
from api.config import load_settings
from api.helpers import _redact_text, _sanitize_error, bad, j, t
from api.models import Session, all_sessions, get_session
from api.profiles import _profiles_match, get_active_profile_name

logger = logging.getLogger(__name__)

DEFAULT_PET_SKIN_ID = "keeper"
_PET_ACTION_TEXT_MAX_CHARS = 140
_PET_NAVIGATION_COMMANDS: list[dict] = []
_PET_NAVIGATION_TTL_SECONDS = 60
_PET_NAVIGATION_MAX_COMMANDS = 20
_PET_NAVIGATION_LOCK = threading.Lock()
_PET_LAUNCH_LOCK = threading.Lock()
_PET_INSTALL_LOCK = threading.Lock()


def _all_profiles_query_flag(parsed_url) -> bool:
    raw = parse_qs(parsed_url.query).get("all_profiles", [""])[0].strip().lower()
    return raw in ("1", "true", "yes", "on")


def _pet_static_path(*parts: str) -> Path:
    return (Path(__file__).parent.parent / "static" / Path(*parts)).resolve()


def _repo_root() -> Path:
    return Path(__file__).parent.parent.resolve()


def _pet_client_is_loopback(handler) -> bool:
    try:
        address = getattr(handler, "client_address", None)
        if not address:
            return False
        return ip_address(str(address[0])).is_loopback
    except Exception:
        return False


def _pet_message_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    if message.get("hidden") or message.get("is_hidden"):
        return ""
    value = message.get("content")
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                part_type = str(item.get("type") or "text")
                if part_type not in ("text", "output_text"):
                    continue
                if isinstance(item.get("text"), str):
                    parts.append(item.get("text"))
        text = "\n".join(parts)
    else:
        text = ""
    return " ".join(text.split()).strip()


def _pet_bubble_text(text: str) -> str:
    lines = []
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line or line in {"---", "***", "___"}:
            continue
        line = line.lstrip("#").strip()
        line = line.lstrip(">•-*0123456789.、)） ").strip()
        if line:
            lines.append(line)
    cleaned = " ".join(" ".join(lines).split()).strip()
    while cleaned and not cleaned[0].isalnum():
        cleaned = cleaned[1:].strip()
    return cleaned


def _pet_truncate_text(text: str, max_chars: int = _PET_ACTION_TEXT_MAX_CHARS) -> str:
    cleaned = " ".join(str(text or "").split()).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    clipped = cleaned[: max(0, max_chars - 3)].rstrip()
    boundary = max(clipped.rfind(" "), clipped.rfind("，"), clipped.rfind("。"), clipped.rfind("、"), clipped.rfind(","))
    if boundary >= max_chars // 2:
        clipped = clipped[:boundary].rstrip()
    return f"{clipped}..."


def _pet_latest_assistant_final_text(session_id: str) -> str:
    try:
        session = Session.load(session_id)
    except Exception:
        session = None
    messages = getattr(session, "messages", None)
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "assistant":
            text = _pet_message_text(message)
            if text:
                return _redact_text(_pet_bubble_text(text))
    return ""


def _pet_session_is_running(session: dict) -> bool:
    return bool(
        session.get("is_streaming")
        or session.get("active_stream_id")
        or session.get("pending_user_message")
        or session.get("has_pending_user_message")
    )


def _pet_source_value(value: object) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    if text.endswith(" session"):
        text = text[: -len(" session")].strip()
    return text


def _pet_session_is_external(session: dict) -> bool:
    if not isinstance(session, dict):
        return False
    session_source = _pet_source_value(session.get("session_source"))
    if session_source in {"messaging", "external-agent", "external agent", "cli"}:
        return True
    source_values = {
        _pet_source_value(session.get("source")),
        _pet_source_value(session.get("source_tag")),
        _pet_source_value(session.get("raw_source")),
        _pet_source_value(session.get("source_label")),
        _pet_source_value(session.get("platform")),
    }
    messaging_sources = {source.replace("_", "-") for source in MESSAGING_SOURCES}
    if source_values & messaging_sources:
        return True
    if is_cli_session_row(session):
        return True
    return bool(session.get("is_cli_session") or session.get("read_only"))


def _pet_pending_approval(session_id: str) -> tuple[dict | None, int]:
    if not session_id:
        return None, 0
    try:
        from tools.approval import _lock as approval_lock
        from tools.approval import _pending as approval_pending
    except Exception:
        return None, 0
    try:
        with approval_lock:
            queue = approval_pending.get(session_id)
            if isinstance(queue, list):
                pending = dict(queue[0]) if queue else None
                return pending, len(queue)
            if queue:
                return dict(queue), 1
    except Exception:
        logger.debug("failed to inspect pet approval state for %s", session_id, exc_info=True)
    return None, 0


def _pet_pending_clarify(session_id: str) -> tuple[dict | None, int]:
    if not session_id:
        return None, 0
    try:
        from api import clarify

        pending = clarify.get_pending(session_id)
        if pending:
            return dict(pending), 1
    except Exception:
        logger.debug("failed to inspect pet clarify state for %s", session_id, exc_info=True)
    return None, 0


def _pet_action_required(session_id: str) -> dict | None:
    approval, approval_count = _pet_pending_approval(session_id)
    if approval:
        text = str(approval.get("description") or approval.get("command") or "").strip()
        if approval.get("command") and approval.get("description"):
            text = f"{approval.get('description')}: {approval.get('command')}"
        text = _pet_truncate_text(_redact_text(_pet_bubble_text(text))) or "请审批此会话"
        return {
            "type": "approval",
            "count": approval_count,
            "text": f"需要批准：{text}",
        }
    clarify, clarify_count = _pet_pending_clarify(session_id)
    if clarify:
        text = str(clarify.get("question") or clarify.get("description") or "").strip()
        text = _pet_truncate_text(_redact_text(_pet_bubble_text(text))) or "请处理这个会话"
        return {
            "type": "clarify",
            "count": clarify_count,
            "text": f"需要选择：{text}",
        }
    return None


def _pet_latest_visible_assistant_process_text(session: dict) -> str:
    sid = str(session.get("session_id") or "")
    run_id = str(session.get("active_stream_id") or "")
    if not sid or not run_id:
        return ""
    try:
        from api.run_journal import read_run_events

        journal = read_run_events(sid, run_id)
    except Exception:
        return ""
    segments: list[str] = []
    current: list[str] = []

    def flush_current() -> None:
        text = "".join(current).strip()
        if text:
            segments.append(text)
        current.clear()

    for event in journal.get("events") or []:
        if not isinstance(event, dict):
            continue
        name = str(event.get("event") or event.get("type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if name == "token":
            text = str(payload.get("text") or "")
            if text:
                current.append(text)
            continue
        if name == "interim_assistant":
            if payload.get("already_streamed"):
                flush_current()
                continue
            flush_current()
            text = str(payload.get("text") or "").strip()
            if text:
                segments.append(text)
            continue
        if name == "tool":
            flush_current()
            continue
    flush_current()
    text = _pet_bubble_text(segments[-1]) if segments else ""
    return _redact_text(text) if text else ""


def _display_rows_without_stale_pet_streams(session_rows) -> list[dict]:
    """Return pet-only rows with dead stream ids hidden without mutating sessions."""
    display_rows: list[dict] = []
    for row in session_rows:
        if not isinstance(row, dict):
            continue
        display_row = dict(row)
        sid = row.get("session_id")
        stream_id = row.get("active_stream_id")
        if not sid or not stream_id or row.get("is_streaming") is True:
            display_rows.append(display_row)
            continue
        with STREAMS_LOCK:
            stream_alive = stream_id in STREAMS
        if stream_alive:
            display_rows.append(display_row)
            continue
        display_row["active_stream_id"] = None
        display_row["is_streaming"] = False
        display_row["pending_user_message"] = None
        display_row["has_pending_user_message"] = False
        display_rows.append(display_row)
    return display_rows


def _pet_attention_session(session: dict) -> dict:
    sid = str(session.get("session_id") or "")
    title = _redact_text(str(session.get("display_title") or session.get("title") or "Session"))
    running = _pet_session_is_running(session)
    action_required = _pet_action_required(sid) if sid else None
    process_text = action_required.get("text") if action_required else (
        _pet_latest_visible_assistant_process_text(session)
        if running
        else (_pet_latest_assistant_final_text(sid) if sid else "")
    )
    if not process_text and running:
        process_text = "正在思考"
    return {
        "session_id": sid,
        "title": title,
        "message_count": int(session.get("message_count") or 0),
        "last_message_at": session.get("last_message_at") or session.get("updated_at") or 0,
        "updated_at": session.get("updated_at") or 0,
        "running": running,
        "action_required": bool(action_required),
        "action_required_type": action_required.get("type") if action_required else "",
        "action_required_count": int(action_required.get("count") or 0) if action_required else 0,
        "process_text": process_text,
        "is_cli_session": bool(session.get("is_cli_session")),
        "source_label": session.get("source_label") or session.get("source_tag") or "",
    }


def _handle_pet_page(handler, template: str = "index.html") -> bool:
    try:
        from api.auth import csrf_token_for_session, is_auth_enabled, parse_cookie, verify_session
        from api.updates import WEBUI_VERSION

        if template not in {"index.html", "bubbles.html"}:
            return bad(handler, "unknown desktop pet page", status=404)
        version_token = quote(WEBUI_VERSION, safe="")
        csrf_token = ""
        try:
            if is_auth_enabled():
                cookie_val = parse_cookie(handler)
                if cookie_val and verify_session(cookie_val):
                    csrf_token = csrf_token_for_session(cookie_val) or ""
        except Exception:
            csrf_token = ""
        html = (
            _pet_static_path("desktop_pet", template)
            .read_text(encoding="utf-8")
            .replace("__WEBUI_VERSION__", version_token)
            .replace("__CSRF_TOKEN_JSON__", json.dumps(csrf_token))
        )
        return t(handler, html, content_type="text/html; charset=utf-8")
    except Exception as exc:
        logger.exception("failed to serve desktop pet")
        return j(handler, {"error": _sanitize_error(exc)}, status=500)


def _handle_pet_attention(handler, parsed) -> bool:
    try:
        limit = int(parse_qs(parsed.query).get("limit", ["30"])[0])
    except (TypeError, ValueError):
        limit = 30
    limit = min(50, max(1, limit))
    rows = _display_rows_without_stale_pet_streams(all_sessions())
    rows_by_sid = {str(s.get("session_id") or ""): s for s in rows if s.get("session_id")}
    try:
        from api import config as _live_config

        with _live_config.ACTIVE_RUNS_LOCK:
            active_runs = [dict(raw or {}) for raw in (_live_config.ACTIVE_RUNS or {}).values()]
        for run in active_runs:
            sid = str(run.get("session_id") or "").strip()
            stream_id = str(run.get("stream_id") or "").strip()
            if not sid:
                continue
            try:
                session = get_session(sid, metadata_only=True)
                row = session.compact(include_runtime=True, active_stream_ids={stream_id} if stream_id else set())
            except Exception:
                row = {"session_id": sid}
            if stream_id:
                row["active_stream_id"] = stream_id
                row["is_streaming"] = True
            if run.get("started_at"):
                row["updated_at"] = max(float(row.get("updated_at") or 0), float(run.get("started_at") or 0))
                row["last_message_at"] = max(float(row.get("last_message_at") or 0), float(run.get("started_at") or 0))
            rows_by_sid[sid] = {**rows_by_sid.get(sid, {}), **row}
        rows = list(rows_by_sid.values())
    except Exception:
        logger.debug("failed to merge active runs into pet attention rows", exc_info=True)

    rows, active_profile = _filter_pet_attention_rows(rows, parsed)
    items = [_pet_attention_session(s) for s in rows]
    items.sort(
        key=lambda item: (
            3 if item.get("action_required") else (2 if item.get("running") else 1),
            item.get("last_message_at") or item.get("updated_at") or 0,
        ),
        reverse=True,
    )
    items = items[:limit]
    return j(handler, {"sessions": items, "active_profile": active_profile, "server_time": time.time()})


def _filter_pet_attention_rows(rows: list[dict], parsed) -> tuple[list[dict], str]:
    active_profile = get_active_profile_name()
    if not _all_profiles_query_flag(parsed):
        rows = [s for s in rows if _profiles_match(s.get("profile"), active_profile)]
    rows = [s for s in rows if not s.get("archived")]
    settings = load_settings()
    show_external_sessions = bool(settings.get("show_cli_sessions"))
    if show_external_sessions:
        rows = [
            s
            for s in rows
            if not _pet_session_is_external(s) or is_cli_session_row_visible(s)
        ]
    else:
        rows = [s for s in rows if not _pet_session_is_external(s)]
    return rows, active_profile


def _pet_skin_url(skin_id: str, sprite_rel: str) -> str:
    parts = [quote(part, safe="") for part in Path(sprite_rel).parts]
    return f"/static/pets/{quote(skin_id, safe='')}/{'/'.join(parts)}"


def _available_pet_skins() -> list[dict]:
    pets_root = _pet_static_path("pets")
    if not pets_root.exists():
        return []
    skins = []
    for skin_dir in sorted(p for p in pets_root.iterdir() if p.is_dir()):
        manifest_path = skin_dir / "pet.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("failed to read pet skin manifest %s", manifest_path, exc_info=True)
            continue
        skin_id = str(manifest.get("id") or skin_dir.name).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", skin_id) or skin_id != skin_dir.name:
            continue
        sprite_rel = str(manifest.get("spritesheetPath") or "spritesheet.webp").strip()
        sprite_parts = Path(sprite_rel).parts
        if not sprite_rel or Path(sprite_rel).is_absolute() or ".." in sprite_parts:
            continue
        sprite_path = (skin_dir / sprite_rel).resolve()
        try:
            sprite_path.relative_to(skin_dir.resolve())
        except ValueError:
            continue
        if not sprite_path.is_file():
            continue
        skins.append(
            {
                "id": skin_id,
                "displayName": str(manifest.get("displayName") or skin_id).strip() or skin_id,
                "description": str(manifest.get("description") or ""),
                "spritesheetPath": sprite_rel,
                "spritesheetUrl": _pet_skin_url(skin_id, sprite_rel),
            }
        )
    skins.sort(key=lambda item: (item["id"] != DEFAULT_PET_SKIN_ID, item["displayName"].lower()))
    return skins


def _handle_pet_skins(handler, parsed) -> bool:
    return j(handler, {"skins": _available_pet_skins(), "default": DEFAULT_PET_SKIN_ID, "server_time": time.time()})


def _desktop_pet_processes() -> list[dict]:
    patterns = ("hermes-desktop-pet", "Hermes Desktop Pet")
    processes: list[dict] = []
    try:
        if os.name == "nt":
            return _desktop_pet_windows_processes()
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return processes
        current_pid = str(os.getpid())
        known_paths = _desktop_pet_known_process_paths()
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(None, 1)
            if not parts or parts[0] == current_pid:
                continue
            command = parts[1] if len(parts) > 1 else ""
            try:
                first_arg = shlex.split(command)[0] if command else ""
            except ValueError:
                first_arg = command.split(None, 1)[0] if command else ""
            command_name = Path(first_arg).name
            first_path = str(Path(first_arg).expanduser().resolve()) if first_arg.startswith("/") else first_arg
            known_app_exec = any(path and path in command for path in known_paths if "/Hermes Desktop Pet.app/" in path)
            if command_name in patterns and first_path in known_paths:
                processes.append({"pid": int(parts[0]), "command": command})
            elif known_app_exec:
                processes.append({"pid": int(parts[0]), "command": command})
    except Exception:
        logger.debug("failed to inspect desktop pet process", exc_info=True)
    return processes


def _desktop_pet_windows_processes() -> list[dict]:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell:
        return _desktop_pet_windows_processes_from_powershell(powershell)
    return _desktop_pet_windows_processes_from_tasklist()


def _desktop_pet_windows_processes_from_powershell(powershell: str) -> list[dict]:
    script = (
        "$items = Get-CimInstance Win32_Process -Filter \"Name = 'hermes-desktop-pet.exe'\" "
        "| Select-Object ProcessId,ExecutablePath,CommandLine; "
        "$items | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        logger.debug("failed to inspect Windows desktop pet processes with PowerShell", exc_info=True)
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.debug("failed to parse Windows desktop pet process JSON: %s", result.stdout.strip())
        return []
    rows = payload if isinstance(payload, list) else [payload]
    known_paths = {_normalize_process_path(path) for path in _desktop_pet_known_process_paths()}
    processes: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            pid = 0
        executable = str(row.get("ExecutablePath") or "").strip()
        if not pid or _normalize_process_path(executable) not in known_paths:
            continue
        command = str(row.get("CommandLine") or executable or "hermes-desktop-pet.exe").strip()
        processes.append({"pid": pid, "command": command, "verified_path": True})
    return processes


def _desktop_pet_windows_processes_from_tasklist() -> list[dict]:
    processes: list[dict] = []
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq hermes-desktop-pet.exe", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        logger.debug("failed to inspect Windows desktop pet processes with tasklist", exc_info=True)
        return processes
    for line in result.stdout.splitlines():
        if "hermes-desktop-pet.exe" not in line.lower():
            continue
        parts = [part.strip().strip('"') for part in line.split(",")]
        pid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if pid:
            processes.append({"pid": pid, "command": line, "verified_path": False})
    return processes


def _normalize_process_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(str(path or "").strip().strip('"')))


def _desktop_pet_known_process_paths() -> set[str]:
    root = _repo_root()
    desktop_pet_dir = root / "desktop-pet"
    exe_name = "hermes-desktop-pet.exe" if os.name == "nt" else "hermes-desktop-pet"
    paths: set[str] = set()
    if sys.platform == "darwin":
        for app_path in (
            Path("/Applications/Hermes Desktop Pet.app"),
            Path.home() / "Applications" / "Hermes Desktop Pet.app",
            desktop_pet_dir / "src-tauri" / "target" / "release" / "bundle" / "macos" / "Hermes Desktop Pet.app",
            desktop_pet_dir / "src-tauri" / "target" / "debug" / "bundle" / "macos" / "Hermes Desktop Pet.app",
        ):
            paths.add(str((app_path / "Contents" / "MacOS" / "hermes-desktop-pet").resolve()))
    for profile in ("release", "debug"):
        paths.add(str((desktop_pet_dir / "src-tauri" / "target" / profile / exe_name).resolve()))
    return paths


def _desktop_pet_process_running() -> bool:
    return bool(_desktop_pet_processes())


def _close_desktop_pet_processes() -> dict:
    with _PET_LAUNCH_LOCK:
        processes = _desktop_pet_processes()
        if not processes:
            return {"ok": True, "closed": 0, "running": False}
        if os.name == "nt":
            closed = 0
            errors = []
            for process in processes:
                if process.get("verified_path") is not True:
                    continue
                pid = int(process.get("pid") or 0)
                if not pid:
                    continue
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    closed += 1
                else:
                    errors.append(_sanitize_error(result.stderr or result.stdout or f"taskkill failed for PID {pid}"))
            if not closed and not errors:
                errors.append("No verified desktop pet process ids were available to close.")
            return {
                "ok": not errors,
                "closed": closed,
                "running": _desktop_pet_process_running(),
                "error": "\n".join(errors),
            }
        closed = 0
        for process in processes:
            pid = int(process.get("pid") or 0)
            if not pid:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                closed += 1
            except ProcessLookupError:
                closed += 1
            except Exception:
                logger.debug("failed to terminate desktop pet pid %s", pid, exc_info=True)
        deadline = time.time() + 2
        while time.time() < deadline:
            if not _desktop_pet_process_running():
                return {"ok": True, "closed": closed, "running": False}
            time.sleep(0.1)
        for process in _desktop_pet_processes():
            pid = int(process.get("pid") or 0)
            if not pid:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                logger.debug("failed to kill desktop pet pid %s", pid, exc_info=True)
        return {"ok": True, "closed": closed, "running": _desktop_pet_process_running()}


def _desktop_pet_shell_source_mtime() -> float:
    root = _repo_root()
    src_dir = root / "desktop-pet" / "src-tauri"
    paths = [
        root / "desktop-pet" / "package.json",
        src_dir / "Cargo.toml",
        src_dir / "build.rs",
        src_dir / "tauri.conf.json",
        src_dir / "src" / "main.rs",
    ]
    capabilities = src_dir / "capabilities"
    if capabilities.is_dir():
        paths.extend(capabilities.glob("*.json"))
    mtimes = []
    for path in paths:
        try:
            if path.is_file():
                mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else 0.0


def _desktop_pet_app_executable(app_path: Path) -> Path:
    exe_name = "hermes-desktop-pet.exe" if os.name == "nt" else "hermes-desktop-pet"
    if sys.platform == "darwin":
        return app_path / "Contents" / "MacOS" / exe_name
    return app_path


def _desktop_pet_artifact_mtime(candidate: dict) -> float:
    artifact = candidate.get("artifact")
    if not artifact:
        return 0.0
    try:
        path = Path(str(artifact))
        return path.stat().st_mtime if path.exists() else 0.0
    except OSError:
        return 0.0


def _desktop_pet_candidate_is_current(candidate: dict, source_mtime: float | None = None) -> bool:
    if candidate.get("kind") == "tauri-dev":
        return True
    artifact_mtime = _desktop_pet_artifact_mtime(candidate)
    if artifact_mtime <= 0:
        return False
    return artifact_mtime >= (source_mtime if source_mtime is not None else _desktop_pet_shell_source_mtime())


def _desktop_pet_launch_candidates(*, include_stale: bool = False, include_dev: bool = False) -> list[dict]:
    root = _repo_root()
    desktop_pet_dir = root / "desktop-pet"
    exe_name = "hermes-desktop-pet.exe" if os.name == "nt" else "hermes-desktop-pet"
    source_mtime = _desktop_pet_shell_source_mtime()
    candidates: list[dict] = []

    def add_candidate(candidate: dict) -> None:
        artifact_mtime = _desktop_pet_artifact_mtime(candidate)
        current = _desktop_pet_candidate_is_current(candidate, source_mtime)
        enriched = {
            **candidate,
            "source_mtime": source_mtime,
            "artifact_mtime": artifact_mtime,
            "stale": not current,
        }
        if current or include_stale:
            candidates.append(enriched)

    if sys.platform == "darwin":
        for app_path in (
            Path("/Applications/Hermes Desktop Pet.app"),
            Path.home() / "Applications" / "Hermes Desktop Pet.app",
            desktop_pet_dir / "src-tauri" / "target" / "release" / "bundle" / "macos" / "Hermes Desktop Pet.app",
            desktop_pet_dir / "src-tauri" / "target" / "debug" / "bundle" / "macos" / "Hermes Desktop Pet.app",
        ):
            app_executable = _desktop_pet_app_executable(app_path)
            if app_executable.is_file():
                add_candidate({"kind": "app", "argv": [str(app_executable)], "cwd": root, "artifact": app_executable})
    for profile in ("release", "debug"):
        binary = desktop_pet_dir / "src-tauri" / "target" / profile / exe_name
        if binary.is_file():
            add_candidate({"kind": f"{profile}-binary", "argv": [str(binary)], "cwd": root, "artifact": binary})
    if include_dev:
        npm = shutil.which("npm")
        if npm and (desktop_pet_dir / "package.json").is_file() and (desktop_pet_dir / "node_modules").is_dir():
            candidates.append({"kind": "tauri-dev", "argv": [npm, "run", "dev"], "cwd": desktop_pet_dir, "source_mtime": source_mtime, "artifact_mtime": 0.0, "stale": False})
    return candidates


def _run_pet_setup_command(argv: list[str], cwd: Path, *, timeout: int) -> dict:
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Timed out running {' '.join(argv[:2])}"}
    except Exception as exc:
        return {"ok": False, "error": _sanitize_error(exc)}
    if result.returncode == 0:
        return {"ok": True}
    output = "\n".join(part.strip() for part in (result.stderr, result.stdout) if part and part.strip())
    return {"ok": False, "error": _sanitize_error(output or f"Command failed: {' '.join(argv)}")}


def _prepare_desktop_pet_shell() -> dict:
    with _PET_INSTALL_LOCK:
        existing = _desktop_pet_launch_candidates()
        if existing:
            return {
                "ok": True,
                "installed": True,
                "method": existing[0]["kind"],
                "steps": ["found-shell", "loaded-assets"],
            }
        root = _repo_root()
        desktop_pet_dir = root / "desktop-pet"
        if not desktop_pet_dir.is_dir():
            return {"ok": False, "error": "Desktop pet source is missing."}
        npm = shutil.which("npm")
        if npm and (desktop_pet_dir / "package.json").is_file() and (desktop_pet_dir / "node_modules").is_dir():
            result = _run_pet_setup_command([npm, "run", "build"], desktop_pet_dir, timeout=600)
        else:
            cargo = shutil.which("cargo")
            if not cargo:
                return {"ok": False, "error": "Rust cargo is required to build the desktop pet shell."}
            result = _run_pet_setup_command(
                [cargo, "build", "--manifest-path", str(desktop_pet_dir / "src-tauri" / "Cargo.toml")],
                root,
                timeout=600,
            )
        if not result.get("ok"):
            return result
        candidates = _desktop_pet_launch_candidates()
        if not candidates:
            return {"ok": False, "error": "Desktop pet build completed, but no launchable shell was found."}
        return {
            "ok": True,
            "installed": True,
            "method": candidates[0]["kind"],
            "steps": ["built-shell", "loaded-assets"],
        }


def _desktop_pet_launch_env(base_url: str) -> dict[str, str]:
    env = os.environ.copy()
    if base_url:
        env["HERMES_DESKTOP_PET_BASE_URL"] = base_url
        env["HERMES_WEBUI_BASE_URL"] = base_url
    return env


def _launch_desktop_pet_process(base_url: str = "") -> dict:
    with _PET_LAUNCH_LOCK:
        if _desktop_pet_process_running():
            return {"ok": True, "already_running": True, "method": "existing", "base_url": base_url}
        candidates = _desktop_pet_launch_candidates()
        if not candidates:
            return {
                "ok": False,
                "error": "Desktop pet shell is not built or installed.",
                "hint": "Build it from desktop-pet/ or install the packaged app, then try again.",
            }
        log_dir = Path.home() / ".hermes" / "webui"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "desktop-pet-launch.log"
        last_error = ""
        for candidate in candidates:
            log_file = None
            try:
                log_file = log_path.open("ab")
                process = subprocess.Popen(
                    candidate["argv"],
                    cwd=str(candidate["cwd"]),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=(os.name != "nt"),
                    env=_desktop_pet_launch_env(base_url),
                )
                return {
                    "ok": True,
                    "already_running": False,
                    "method": candidate["kind"],
                    "pid": process.pid,
                    "base_url": base_url,
                }
            except Exception as exc:
                last_error = _sanitize_error(exc)
                logger.debug("desktop pet launch candidate failed: %s", candidate.get("kind"), exc_info=True)
            finally:
                if log_file is not None:
                    try:
                        log_file.close()
                    except Exception:
                        pass
        return {"ok": False, "error": last_error or "Desktop pet launch failed."}


def _handle_pet_launch(handler, body: dict) -> bool:
    if not _pet_client_is_loopback(handler):
        return bad(handler, "desktop pet launch is only available from this machine", status=403)
    try:
        scheme, host = _pet_request_base(handler)
        result = _launch_desktop_pet_process(f"{scheme}://{host}")
        status = 200 if result.get("ok") else 409
        return j(handler, result, status=status)
    except Exception as exc:
        logger.exception("failed to launch desktop pet")
        return j(handler, {"ok": False, "error": _sanitize_error(exc)}, status=500)


def _handle_pet_install(handler, body: dict) -> bool:
    if not _pet_client_is_loopback(handler):
        return bad(handler, "desktop pet install is only available from this machine", status=403)
    try:
        result = _prepare_desktop_pet_shell()
        return j(handler, result, status=200 if result.get("ok") else 409)
    except Exception as exc:
        logger.exception("failed to prepare desktop pet")
        return j(handler, {"ok": False, "error": _sanitize_error(exc)}, status=500)


def _handle_pet_status(handler, body: dict) -> bool:
    if not _pet_client_is_loopback(handler):
        return bad(handler, "desktop pet status is only available from this machine", status=403)
    try:
        candidates = _desktop_pet_launch_candidates()
        stale_candidates = _desktop_pet_launch_candidates(include_stale=True)
        first = candidates[0] if candidates else (stale_candidates[0] if stale_candidates else {})
        return j(
            handler,
            {
                "ok": True,
                "installed": bool(candidates),
                "running": _desktop_pet_process_running(),
                "method": first.get("kind", ""),
                "stale": bool(stale_candidates) and not bool(candidates),
                "source_mtime": _desktop_pet_shell_source_mtime(),
                "artifact_mtime": first.get("artifact_mtime", 0.0),
            },
        )
    except Exception as exc:
        logger.exception("failed to inspect desktop pet status")
        return j(handler, {"ok": False, "error": _sanitize_error(exc)}, status=500)


def _handle_pet_close(handler, body: dict) -> bool:
    if not _pet_client_is_loopback(handler):
        return bad(handler, "desktop pet close is only available from this machine", status=403)
    try:
        result = _close_desktop_pet_processes()
        return j(handler, result, status=200 if result.get("ok") else 409)
    except Exception as exc:
        logger.exception("failed to close desktop pet")
        return j(handler, {"ok": False, "error": _sanitize_error(exc)}, status=500)


def _pet_request_base(handler) -> tuple[str, str]:
    scheme = str(handler.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip().lower()
    if scheme not in {"http", "https"}:
        scheme = "http"
    host = str(handler.headers.get("Host") or "127.0.0.1:8787").split(",")[0].strip().lower()
    if host.startswith("0.0.0.0"):
        host = "127.0.0.1" + host[len("0.0.0.0") :]
    elif host.startswith("[::1]"):
        host = "localhost" + host[len("[::1]") :]
    elif host.startswith("::1"):
        host = "localhost" + host[len("::1") :]
    loopback_host = re.fullmatch(r"(localhost|127(?:\.[0-9]{1,3}){3}|\[::1\]|::1)(:[0-9]{1,5})?", host)
    if not loopback_host:
        host = "127.0.0.1:8787"
    return scheme, host


def _pet_open_url(handler, session_id: str, *, draft: str = "", autosend: bool = False) -> str:
    sid = str(session_id or "").strip()
    if not sid or not re.fullmatch(r"[A-Za-z0-9_.-]+", sid):
        raise ValueError("invalid session_id")
    scheme, host = _pet_request_base(handler)
    query = {}
    if draft:
        query["draft"] = str(draft)
    suffix = ("?" + urlencode(query)) if query else ""
    return f"{scheme}://{host}/session/{quote(sid, safe='')}{suffix}"


def _queue_pet_session_navigation(handler, body: dict) -> dict:
    sid = str(body.get("session_id") or "").strip()
    url = _pet_open_url(
        handler,
        sid,
        draft=str(body.get("draft") or ""),
        autosend=bool(body.get("autosend")),
    )
    try:
        get_session(sid, metadata_only=True)
    except KeyError:
        raise ValueError("session not found")
    command = {
        "id": uuid.uuid4().hex,
        "session_id": sid,
        "draft": str(body.get("draft") or ""),
        "autosend": bool(body.get("autosend")),
        "url": url,
        "created_at": time.time(),
    }
    with _PET_NAVIGATION_LOCK:
        _PET_NAVIGATION_COMMANDS.append(command)
        _trim_pet_navigation_commands_locked(now=command["created_at"])
    return command


def _queue_and_focus_pet_session_navigation(handler, body: dict) -> dict:
    command = _queue_pet_session_navigation(handler, body)
    focused = False
    if sys.platform == "darwin":
        focused = _focus_existing_pet_browser_tab(command.get("url", ""))
    command["focused"] = bool(focused)
    return command


def _handle_pet_navigation(handler, parsed) -> bool:
    since = str(parse_qs(parsed.query).get("since", [""])[0] or "")
    now = time.time()
    with _PET_NAVIGATION_LOCK:
        _trim_pet_navigation_commands_locked(now=now)
        command = _next_pet_navigation_command_locked(since)
    return j(handler, {"command": command or None, "server_time": time.time()})


def _trim_pet_navigation_commands_locked(*, now: float) -> None:
    cutoff = now - _PET_NAVIGATION_TTL_SECONDS
    _PET_NAVIGATION_COMMANDS[:] = [
        command for command in _PET_NAVIGATION_COMMANDS if float(command.get("created_at") or 0) >= cutoff
    ][-_PET_NAVIGATION_MAX_COMMANDS:]


def _next_pet_navigation_command_locked(since: str) -> dict:
    if not _PET_NAVIGATION_COMMANDS:
        return {}
    if not since:
        return dict(_PET_NAVIGATION_COMMANDS[0])
    for index, command in enumerate(_PET_NAVIGATION_COMMANDS):
        if command.get("id") == since:
            next_index = index + 1
            return dict(_PET_NAVIGATION_COMMANDS[next_index]) if next_index < len(_PET_NAVIGATION_COMMANDS) else {}
    return dict(_PET_NAVIGATION_COMMANDS[-1])


def _pet_browser_host_candidates(url: str) -> list[str]:
    parsed = urlparse(url)
    candidates = []
    if parsed.netloc:
        candidates.append(parsed.netloc)
    if parsed.port:
        candidates.extend(
            [
                f"127.0.0.1:{parsed.port}",
                f"localhost:{parsed.port}",
                f"0.0.0.0:{parsed.port}",
            ]
        )
    return list(dict.fromkeys(candidate for candidate in candidates if candidate))


def _run_pet_browser_reuse_script(app_name: str, script: str, url: str, host_candidates: list[str]) -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-e", script, url, *host_candidates],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        logger.debug("failed to run pet browser reuse script for %s", app_name, exc_info=True)
        return False
    if result.returncode != 0:
        logger.debug("pet browser reuse script for %s failed: %s", app_name, result.stderr.strip())
        return False
    return result.stdout.strip() == "reused"


def _reuse_existing_pet_browser_tab(url: str) -> bool:
    host_candidates = _pet_browser_host_candidates(url)
    if not host_candidates:
        return False
    chromium_script_template = r'''
on run argv
  set targetUrl to item 1 of argv
  set hostCandidates to {}
  repeat with idx from 2 to count of argv
    set end of hostCandidates to item idx of argv
  end repeat
  tell application "System Events" to set isRunning to exists (process "{app_name}")
  if isRunning is false then return "not-running"
  tell application "{app_name}"
    repeat with w in windows
      set tabIndex to 1
      repeat with t in tabs of w
        set tabUrl to URL of t
        repeat with hostText in hostCandidates
          if tabUrl contains hostText then
            set URL of t to targetUrl
            set active tab index of w to tabIndex
            set index of w to 1
            activate
            return "reused"
          end if
        end repeat
        set tabIndex to tabIndex + 1
      end repeat
    end repeat
  end tell
  return "not-found"
end run
'''
    safari_script = r'''
on run argv
  set targetUrl to item 1 of argv
  set hostCandidates to {}
  repeat with idx from 2 to count of argv
    set end of hostCandidates to item idx of argv
  end repeat
  tell application "System Events" to set isRunning to exists (process "Safari")
  if isRunning is false then return "not-running"
  tell application "Safari"
    repeat with w in windows
      repeat with t in tabs of w
        set tabUrl to URL of t
        repeat with hostText in hostCandidates
          if tabUrl contains hostText then
            set URL of t to targetUrl
            set current tab of w to t
            set index of w to 1
            activate
            return "reused"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "not-found"
end run
'''
    for app_name in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Arc"):
        script = chromium_script_template.replace("{app_name}", app_name)
        if _run_pet_browser_reuse_script(app_name, script, url, host_candidates):
            return True
    return _run_pet_browser_reuse_script("Safari", safari_script, url, host_candidates)


def _focus_existing_pet_browser_tab(url: str) -> bool:
    host_candidates = _pet_browser_host_candidates(url)
    if not host_candidates:
        return False
    chromium_script_template = r'''
on run argv
  set hostCandidates to {}
  repeat with idx from 2 to count of argv
    set end of hostCandidates to item idx of argv
  end repeat
  tell application "System Events" to set isRunning to exists (process "{app_name}")
  if isRunning is false then return "not-running"
  tell application "{app_name}"
    repeat with w in windows
      set tabIndex to 1
      repeat with t in tabs of w
        set tabUrl to URL of t
        repeat with hostText in hostCandidates
          if tabUrl contains hostText then
            set active tab index of w to tabIndex
            set index of w to 1
            activate
            return "reused"
          end if
        end repeat
        set tabIndex to tabIndex + 1
      end repeat
    end repeat
  end tell
  return "not-found"
end run
'''
    safari_script = r'''
on run argv
  set hostCandidates to {}
  repeat with idx from 2 to count of argv
    set end of hostCandidates to item idx of argv
  end repeat
  tell application "System Events" to set isRunning to exists (process "Safari")
  if isRunning is false then return "not-running"
  tell application "Safari"
    repeat with w in windows
      repeat with t in tabs of w
        set tabUrl to URL of t
        repeat with hostText in hostCandidates
          if tabUrl contains hostText then
            set current tab of w to t
            set index of w to 1
            activate
            return "reused"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "not-found"
end run
'''
    for app_name in ("Google Chrome", "Microsoft Edge", "Brave Browser", "Arc"):
        script = chromium_script_template.replace("{app_name}", app_name)
        if _run_pet_browser_reuse_script(app_name, script, url, host_candidates):
            return True
    if _run_pet_browser_reuse_script("Safari", safari_script, url, host_candidates):
        return True
    return _focus_existing_pet_browser_window_by_title()


def _focus_existing_pet_browser_window_by_title() -> bool:
    script = r'''
on run argv
  set browserNames to {"Google Chrome", "Microsoft Edge", "Brave Browser", "Arc", "Safari"}
  tell application "System Events"
    repeat with appName in browserNames
      if exists (process appName) then
        tell process appName
          repeat with w in windows
            set windowTitle to name of w
            if windowTitle contains "Hermes" then
              set frontmost to true
              try
                perform action "AXRaise" of w
              end try
              return "reused"
            end if
          end repeat
        end tell
      end if
    end repeat
  end tell
  return "not-found"
end run
'''
    return _run_pet_browser_reuse_script("System Events", script, "", [])


def _handle_pet_open_session(handler, body: dict) -> bool:
    if not _pet_client_is_loopback(handler):
        return bad(handler, "desktop pet session navigation is only available from this machine", status=403)
    try:
        command = _queue_and_focus_pet_session_navigation(handler, body)
        return j(
            handler,
            {
                "ok": True,
                "queued": True,
                "opened": False,
                "focused": bool(command.get("focused")),
                "command": command,
                "url": command.get("url", ""),
            },
        )
    except ValueError as exc:
        return bad(handler, str(exc), status=400)
    except Exception as exc:
        logger.exception("failed to open pet session")
        return j(handler, {"ok": False, "url": "", "error": _sanitize_error(exc)}, status=500)


def handle_get(handler, parsed) -> bool:
    if parsed.path == "/pet":
        _handle_pet_page(handler)
        return True
    if parsed.path == "/pet/bubbles":
        _handle_pet_page(handler, "bubbles.html")
        return True
    if parsed.path == "/api/pet/attention":
        _handle_pet_attention(handler, parsed)
        return True
    if parsed.path == "/api/pet/skins":
        _handle_pet_skins(handler, parsed)
        return True
    if parsed.path == "/api/pet/navigation":
        _handle_pet_navigation(handler, parsed)
        return True
    return False


def handle_post(handler, parsed, body: dict) -> bool:
    if parsed.path == "/api/pet/status":
        _handle_pet_status(handler, body)
        return True
    if parsed.path == "/api/pet/install":
        _handle_pet_install(handler, body)
        return True
    if parsed.path == "/api/pet/launch":
        _handle_pet_launch(handler, body)
        return True
    if parsed.path == "/api/pet/close":
        _handle_pet_close(handler, body)
        return True
    if parsed.path == "/api/pet/open_session":
        _handle_pet_open_session(handler, body)
        return True
    return False
