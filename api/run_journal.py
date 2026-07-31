"""Append-only WebUI run event journal helpers.

This is the first #1925 journal/replay slice.  It mirrors SSE events emitted by
the existing in-process streaming path without changing execution ownership.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

RUN_JOURNAL_DIR_NAME = "_run_journal"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_WRITER_LOCKS: dict[tuple[str, str, str], threading.Lock] = {}
_WRITER_LOCKS_GUARD = threading.Lock()
# Next-seq to assign per run-journal file path, kept in memory so repeat appends
# to the same run do not re-parse the whole file on every call. The per-path
# ``_lock_for(path)`` serializes same-path reserve→append so seqs stay monotonic
# and file order matches; ``_SEQ_CACHE_LOCK`` (below) additionally guards every
# *structural* access to the dict (reserve/note/evict) so ``delete_run_journal``
# can iterate + drop keys while a concurrent append on ANOTHER path inserts one,
# without a ``dictionary changed size during iteration`` crash. See
# ``_reserve_next_seq`` and ``delete_run_journal`` (which evicts stale entries).
_SEQ_CACHE: dict[str, int] = {}
_SEQ_CACHE_LOCK = threading.Lock()
# Summary callers only need terminal state and the latest cursor. Re-parsing a
# completed journal's full payload (which can include multi-megabyte tool or
# session results) on every status/reconnect probe is needless. This process
# cache is keyed by a complete stat identity, so it is never used after an
# atomic replacement, append, truncate, or same-path file recreation.
_SUMMARY_CACHE_MAX_ENTRIES = 128
_SUMMARY_CACHE: OrderedDict[str, tuple[tuple[int, int, int, int, int], dict]] = OrderedDict()
_SUMMARY_CACHE_LOCK = threading.Lock()
# Events that mark a run terminal in the journal / summary sense.
TERMINAL_SSE_EVENTS = frozenset({"done", "cancel", "apperror", "error", "stream_end"})
# Events that should close an SSE relay drain loop. `done` is intentionally
# excluded: background title generation and `stream_end` are emitted after
# `done`, and breaking early would drop them. `apperror` is included because
# it terminates with no trailing `stream_end`.
SSE_RELAY_CLOSE_EVENTS = frozenset({"stream_end", "cancel", "apperror", "error"})
# Back-compat alias used by older call sites / tests.
_TERMINAL_SSE_EVENTS = TERMINAL_SSE_EVENTS
_FSYNC_MODE_ENV = "HERMES_WEBUI_RUN_JOURNAL_FSYNC"
_FSYNC_MODE_EAGER = "eager"
_FSYNC_MODE_TERMINAL_ONLY = "terminal-only"
_SESSION_REPLAY_MAX_BYTES = 4 * 1024 * 1024
_SESSION_REPLAY_MAX_ROWS = 4096
_SESSION_REPLAY_READ_CHUNK_BYTES = 64 * 1024
_LEGACY_TERMINAL_RECOVERY_MAX_BYTES = 16 * 1024 * 1024
_SNAPSHOT_ARGS_MAX_ITEMS = 64
_SNAPSHOT_ARGS_MAX_DEPTH = 8
_SNAPSHOT_ARGS_MAX_STRING_CHARS = 8192
_SNAPSHOT_ARGS_MAX_TOTAL_CHARS = 64 * 1024
_SNAPSHOT_ARGS_TRUNCATED_SUFFIX = "...[truncated]"


def _default_session_dir() -> Path:
    from api.models import SESSION_DIR

    return Path(SESSION_DIR)


def _validate_id(value: str, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or not _SAFE_ID_RE.fullmatch(cleaned):
        raise ValueError(f"invalid {field}")
    return cleaned


def _run_path(session_id: str, run_id: str, session_dir: Path | None = None) -> Path:
    sid = _validate_id(session_id, "session_id")
    rid = _validate_id(run_id, "run_id")
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    return root / RUN_JOURNAL_DIR_NAME / sid / f"{rid}.jsonl"


def _lock_for(path: Path) -> threading.Lock:
    key = (str(path.parent), path.name, str(os.getpid()))
    with _WRITER_LOCKS_GUARD:
        lock = _WRITER_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITER_LOCKS[key] = lock
        return lock


def _summary_cache_signature(path: Path) -> tuple[int, int, int, int, int] | None:
    """Return the complete filesystem identity used for summary-cache validity.

    Includes ``st_ctime_ns`` so a same-inode, same-size rewrite that restores the
    original ``mtime_ns`` (e.g. an atomic replace) still invalidates the cache —
    ctime advances on any metadata/content change and cannot be forged back.
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(stat.st_ctime_ns),
    )


def _get_cached_summary(path: Path) -> dict | None:
    signature = _summary_cache_signature(path)
    if signature is None:
        return None
    key = str(path)
    with _SUMMARY_CACHE_LOCK:
        cached = _SUMMARY_CACHE.get(key)
        if cached is None:
            return None
        cached_signature, summary = cached
        if cached_signature != signature:
            _SUMMARY_CACHE.pop(key, None)
            return None
        _SUMMARY_CACHE.move_to_end(key)
        return dict(summary)


def _cache_summary(
    path: Path,
    summary: dict,
    *,
    expected_signature: tuple[int, int, int, int, int] | None = None,
) -> None:
    signature = _summary_cache_signature(path)
    # The pre-read signature is an enforced TOCTOU precondition. In particular,
    # a journal created after a missing-file read has ``None -> signature`` and
    # must not cache the empty/unknown result under the new file's identity.
    if signature is None or signature != expected_signature:
        return
    key = str(path)
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE[key] = (signature, dict(summary))
        _SUMMARY_CACHE.move_to_end(key)
        while len(_SUMMARY_CACHE) > _SUMMARY_CACHE_MAX_ENTRIES:
            _SUMMARY_CACHE.popitem(last=False)


def _discard_cached_summary(path: Path) -> None:
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE.pop(str(path), None)


def _read_jsonl(path: Path) -> tuple[list[dict], list[dict]]:
    events: list[dict] = []
    malformed: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return events, malformed
    for line_no, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            malformed.append({"line": line_no, "raw": raw})
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
        else:
            malformed.append({"line": line_no, "raw": raw})
    return events, malformed


def _parse_run_journal_event_id(raw: str | None) -> tuple[str | None, int | None]:
    raw = str(raw or "").strip()
    if not raw:
        return None, None
    if ":" in raw:
        run_id, tail = raw.rsplit(":", 1)
    else:
        run_id, tail = None, raw
    try:
        seq = max(0, int(tail))
    except (TypeError, ValueError):
        return run_id or None, None
    return run_id or None, seq


def _snapshot_args_take_budget(budget: dict[str, int], amount: int) -> int:
    remaining = max(0, int(budget.get("remaining") or 0))
    take = min(remaining, max(0, amount))
    budget["remaining"] = remaining - take
    return take


def _bound_snapshot_args_string(value: str, budget: dict[str, int]) -> str:
    max_chars = min(len(value), _SNAPSHOT_ARGS_MAX_STRING_CHARS)
    take = _snapshot_args_take_budget(budget, max_chars)
    out = value[:take]
    if take < len(value):
        suffix_take = _snapshot_args_take_budget(budget, len(_SNAPSHOT_ARGS_TRUNCATED_SUFFIX))
        out += _SNAPSHOT_ARGS_TRUNCATED_SUFFIX[:suffix_take]
    return out


def _bound_run_journal_snapshot_value(value: Any, budget: dict[str, int], depth: int) -> Any:
    if budget.get("remaining", 0) <= 0:
        return None
    if isinstance(value, str):
        return _bound_snapshot_args_string(value, budget)
    if isinstance(value, dict):
        if depth >= _SNAPSHOT_ARGS_MAX_DEPTH:
            return {}
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _SNAPSHOT_ARGS_MAX_ITEMS or budget.get("remaining", 0) <= 0:
                break
            bounded_key = _bound_snapshot_args_string(str(key), budget)
            if not bounded_key:
                continue
            out[bounded_key] = _bound_run_journal_snapshot_value(item, budget, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        if depth >= _SNAPSHOT_ARGS_MAX_DEPTH:
            return []
        return [
            _bound_run_journal_snapshot_value(item, budget, depth + 1)
            for item in value[:_SNAPSHOT_ARGS_MAX_ITEMS]
            if budget.get("remaining", 0) > 0
        ]
    if isinstance(value, (bool, int, float)) or value is None:
        try:
            _snapshot_args_take_budget(budget, len(json.dumps(value)))
        except (TypeError, ValueError):
            return None
        return value
    return _bound_snapshot_args_string(str(value), budget)


def bound_run_journal_snapshot_args(args: Any) -> Any:
    """Return recovery tool args with realistic values intact and pathological payloads bounded."""
    if args is None:
        return {}
    budget = {"remaining": _SNAPSHOT_ARGS_MAX_TOTAL_CHARS}
    return _bound_run_journal_snapshot_value(args, budget, 0)


def _next_seq(path: Path) -> int:
    events, _malformed = _read_jsonl(path)
    seqs = [int(event.get("seq") or 0) for event in events if isinstance(event.get("seq"), int)]
    return (max(seqs) + 1) if seqs else 1


def _reserve_next_seq(path: Path) -> int:
    """Reserve and return the next seq for ``path``, advancing the in-memory cache.

    Callers MUST hold ``_lock_for(path)``. The first append per path in this
    process seeds the cache from ``_next_seq(path)`` (one file read); every later
    append is a pure in-memory increment, avoiding the O(n) re-parse that
    re-reading the whole journal on every append caused (O(n^2) over a run).
    Because ``RunJournalWriter`` and the free ``append_run_event`` share this one
    cache under the same per-path lock, their seqs stay monotonic and gapless
    even when both write the same path. ``_SEQ_CACHE_LOCK`` additionally makes the
    dict get+set atomic against a concurrent cross-path eviction.
    """
    key = str(path)
    with _SEQ_CACHE_LOCK:
        nxt = _SEQ_CACHE.get(key)
        if nxt is not None:
            _SEQ_CACHE[key] = nxt + 1
            return nxt
    # Cache miss: seed from disk WITHOUT holding the module-global lock, so a
    # slow first-access file read for one path can't block every other path's
    # cache ops. The caller holds the per-path lock, so only one thread per path
    # can reach this branch — no double-seed, and no same-path writer can race
    # the value in between.
    seeded = _next_seq(path)
    with _SEQ_CACHE_LOCK:
        _SEQ_CACHE[key] = seeded + 1
        return seeded


def _note_assigned_seq(path: Path, seq: int) -> None:
    """Keep the cache at least one past an explicitly-supplied ``seq``.

    Callers MUST hold ``_lock_for(path)``. When an append carries a caller-chosen
    ``seq`` rather than drawing from the cache, advance the cache so a later
    cache-based append on the same path cannot re-issue an already-used seq.
    """
    key = str(path)
    nxt = int(seq) + 1
    with _SEQ_CACHE_LOCK:
        if _SEQ_CACHE.get(key, 0) < nxt:
            _SEQ_CACHE[key] = nxt


def _terminal_state_for_event(event_name: str, payload) -> str | None:
    name = str(event_name or "")
    if name == "done" or name == "stream_end":
        if isinstance(payload, dict):
            explicit_state = str(payload.get("terminal_state") or "").strip().lower()
            if explicit_state in {"tool_limit_reached"}:
                return explicit_state
        return "completed"
    if name == "cancel":
        return "interrupted-by-user"
    if name in {"apperror", "error"}:
        err_type = str((payload or {}).get("type") or "").strip().lower() if isinstance(payload, dict) else ""
        if err_type == "tool_limit_reached":
            return "tool_limit_reached"
        if err_type in {"cancelled", "canceled"}:
            return "interrupted-by-user"
        if err_type == "interrupted":
            return "interrupted-by-crash"
        return "errored"
    return None


def _run_journal_fsync_mode() -> str:
    raw = os.environ.get(_FSYNC_MODE_ENV, _FSYNC_MODE_TERMINAL_ONLY)
    mode = str(raw or "").strip().lower()
    if mode in {_FSYNC_MODE_EAGER, _FSYNC_MODE_TERMINAL_ONLY}:
        return mode
    return _FSYNC_MODE_TERMINAL_ONLY


def _should_fsync_event(terminal_state: str | None) -> bool:
    if _run_journal_fsync_mode() == _FSYNC_MODE_EAGER:
        return True
    return bool(terminal_state)


def _fsync_parent_dir(path: Path) -> None:
    try:
        dir_fd = os.open(path.parent, getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _event_created_at(event: dict, *, fallback: float = 0.0) -> float:
    try:
        return float(event.get("created_at") or fallback)
    except (TypeError, ValueError):
        return fallback


def _iter_bounded_raw_jsonl_lines(path: Path, *, max_bytes: int, retained_bytes: int = 0):
    line_no = 0
    buffered = bytearray()
    total_bytes = int(retained_bytes)
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(_SESSION_REPLAY_READ_CHUNK_BYTES)
                if not chunk:
                    if buffered:
                        if total_bytes + len(buffered) > max_bytes:
                            raise ValueError("replay_limit_bytes")
                        line_no += 1
                        total_bytes += len(buffered)
                        yield line_no, bytes(buffered), total_bytes
                    return
                start = 0
                while start < len(chunk):
                    newline = chunk.find(b"\n", start)
                    if newline == -1:
                        buffered.extend(chunk[start:])
                        if total_bytes + len(buffered) > max_bytes:
                            raise ValueError("replay_limit_bytes")
                        break
                    buffered.extend(chunk[start : newline + 1])
                    if total_bytes + len(buffered) > max_bytes:
                        raise ValueError("replay_limit_bytes")
                    line_no += 1
                    total_bytes += len(buffered)
                    yield line_no, bytes(buffered), total_bytes
                    buffered.clear()
                    start = newline + 1
    except FileNotFoundError:
        return


def append_run_event(
    session_id: str,
    run_id: str,
    event_name: str,
    payload=None,
    *,
    session_dir: Path | None = None,
    seq: int | None = None,
    created_at: float | None = None,
) -> dict:
    """Append one durable run event and fsync it according to the journal policy."""
    path = _run_path(session_id, run_id, session_dir=session_dir)
    payload = payload if payload is not None else {}
    event_name = str(event_name or "").strip()
    if not event_name:
        raise ValueError("event_name is required")
    with _lock_for(path):
        if seq is not None:
            assigned_seq = int(seq)
            _note_assigned_seq(path, assigned_seq)
        else:
            assigned_seq = _reserve_next_seq(path)
        terminal_state = _terminal_state_for_event(event_name, payload)
        event = {
            "version": 1,
            "event_id": f"{run_id}:{assigned_seq}",
            "seq": assigned_seq,
            "run_id": str(run_id),
            "session_id": str(session_id),
            "event": event_name,
            "type": event_name,
            "created_at": float(created_at if created_at is not None else time.time()),
            "terminal": bool(terminal_state),
            "terminal_state": terminal_state,
            "payload": payload,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        created_file = not path.exists()
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            if _should_fsync_event(terminal_state):
                os.fsync(fh.fileno())
        _discard_cached_summary(path)
        if created_file:
            _fsync_parent_dir(path)
        return event


class RunJournalWriter:
    """Stateful writer for one WebUI stream/run."""

    def __init__(self, session_id: str, run_id: str, *, session_dir: Path | None = None):
        self.session_id = _validate_id(session_id, "session_id")
        self.run_id = _validate_id(run_id, "run_id")
        self.session_dir = Path(session_dir) if session_dir is not None else None
        self._path = _run_path(self.session_id, self.run_id, session_dir=self.session_dir)
        self._lock = _lock_for(self._path)

    def append_sse_event(self, event_name: str, payload=None) -> dict:
        # Draw from the shared module-level seq cache under the per-path lock so
        # this writer and any direct append_run_event() call on the same path
        # agree on one monotonic, gapless sequence.
        with self._lock:
            seq = _reserve_next_seq(self._path)
        return append_run_event(
            self.session_id,
            self.run_id,
            event_name,
            payload or {},
            session_dir=self.session_dir,
            seq=seq,
        )


def _recover_legacy_overcap_terminal_event(
    event: dict,
    *,
    session_id: str,
    run_id: str,
    max_seq: int | None,
) -> dict | None:
    """Convert one bounded-size legacy terminal row into a fixed recovery marker."""
    if not isinstance(event, dict) or event.get("terminal") is not True:
        return None
    try:
        seq = int(event.get("seq") or 0)
    except (TypeError, ValueError):
        return None
    if (
        seq <= 0
        or (max_seq is not None and seq > int(max_seq))
        or str(event.get("event_id") or "") != f"{run_id}:{seq}"
        or str(event.get("run_id") or "") != str(run_id)
        or str(event.get("session_id") or "") != str(session_id)
        or str(event.get("event") or "") not in TERMINAL_SSE_EVENTS
    ):
        return None
    event_name = str(event.get("event") or "")
    terminal_state = str(event.get("terminal_state") or "").strip().lower()
    if terminal_state not in {
        "completed", "interrupted-by-user", "interrupted-by-crash", "errored",
        "tool_limit_reached",
    }:
        terminal_state = _terminal_state_for_event(event_name, {}) or "errored"
    return {
        "version": 1,
        "event_id": f"{run_id}:{seq}",
        "seq": seq,
        "run_id": str(run_id),
        "session_id": str(session_id),
        "event": event_name,
        "type": event_name,
        "terminal": True,
        "terminal_state": terminal_state,
        "payload": {
            "terminal_session_persisted": False,
            "terminal_disposition": {
                "version": "terminal_disposition_v1",
                "kind": "consumed_non_materializable",
                "reason": "legacy_terminal_payload_too_large",
                "session_id": str(session_id),
                "run_id": str(run_id),
                "stream_id": str(run_id),
            },
        },
    }


def _serialized_event_size(event: dict) -> int:
    return len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1


class _TopLevelEnvelopeScanner:
    """Extract cursor identity from one JSON object without retaining its payload.

    Oversized legacy rows cannot be decoded as one allocation. This scanner only
    accepts unique top-level ``seq``/owner fields, so a nested or truncated
    ``"seq"`` can never become replay cursor authority.
    """

    _STRING_FIELDS = frozenset({"event_id", "run_id", "session_id"})
    _CAPTURE_LIMIT = 1024
    _MAX_NESTING = 128

    def __init__(self):
        self._stack: list[int] = []
        self._state = "start"
        self._invalid = False
        self._in_string = False
        self._escape = False
        self._string_role = ""
        self._string_buf = bytearray()
        self._string_overflow = False
        self._active_key: str | None = None
        self._primitive_buf: bytearray | None = None
        self._primitive_overflow = False
        self._fields: dict[str, object] = {}
        self._seq_seen = 0

    @staticmethod
    def _decode_string(raw: bytearray, overflow: bool) -> str | None:
        if overflow:
            return None
        try:
            value = json.loads((b'"' + bytes(raw) + b'"').decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, str) else None

    def _append_string_byte(self, value: int) -> None:
        if self._string_role not in {"key", "value"}:
            return
        if len(self._string_buf) >= self._CAPTURE_LIMIT:
            self._string_overflow = True
            return
        self._string_buf.append(value)

    def _record_field(self, key: str, value) -> None:
        if key == "seq":
            self._seq_seen += 1
        if key in self._fields:
            self._invalid = True
            return
        self._fields[key] = value

    def _finish_string(self) -> None:
        role = self._string_role
        decoded = self._decode_string(self._string_buf, self._string_overflow)
        self._in_string = False
        self._escape = False
        self._string_role = ""
        self._string_buf.clear()
        self._string_overflow = False
        if role == "key":
            self._active_key = decoded
            self._state = "colon"
            return
        if role == "value":
            if self._active_key == "seq":
                self._record_field("seq", None)
            elif self._active_key in self._STRING_FIELDS:
                self._record_field(self._active_key, decoded)
                if decoded is None:
                    self._invalid = True
            self._active_key = None
            self._state = "comma_or_end"

    def _finish_primitive(self) -> None:
        key = self._active_key
        token = bytes(self._primitive_buf or b"").strip()
        if key == "seq":
            if (
                not self._primitive_overflow
                and re.fullmatch(rb"-?(?:0|[1-9]\d*)", token)
            ):
                value = int(token)
            else:
                value = None
            self._record_field("seq", value)
        elif key in self._STRING_FIELDS:
            self._record_field(key, None)
            self._invalid = True
        self._primitive_buf = None
        self._primitive_overflow = False
        self._active_key = None
        self._state = "comma_or_end"

    def _push(self, value: int) -> None:
        if len(self._stack) >= self._MAX_NESTING:
            self._invalid = True
            return
        self._stack.append(value)

    def _pop(self, value: int) -> None:
        expected = ord("{") if value == ord("}") else ord("[")
        if not self._stack or self._stack[-1] != expected:
            self._invalid = True
            return
        self._stack.pop()

    def feed(self, chunk: bytes) -> None:
        if self._invalid:
            return
        for value in chunk:
            if self._in_string:
                if self._escape:
                    self._append_string_byte(value)
                    self._escape = False
                elif value == ord("\\"):
                    self._append_string_byte(value)
                    self._escape = True
                elif value == ord('"'):
                    self._finish_string()
                else:
                    self._append_string_byte(value)
                continue

            if self._primitive_buf is not None:
                if len(self._stack) == 1 and value in {ord(","), ord("}")}:
                    self._finish_primitive()
                else:
                    if len(self._primitive_buf) >= self._CAPTURE_LIMIT:
                        self._primitive_overflow = True
                    else:
                        self._primitive_buf.append(value)
                    continue

            if value in b" \t\r\n":
                continue
            if self._state == "closed":
                self._invalid = True
                return
            if self._state == "start":
                if value != ord("{"):
                    self._invalid = True
                    return
                self._push(value)
                self._state = "key_or_end"
                continue

            depth = len(self._stack)
            if value == ord('"'):
                self._in_string = True
                self._escape = False
                self._string_buf.clear()
                self._string_overflow = False
                if depth == 1 and self._state == "key_or_end":
                    self._string_role = "key"
                elif depth == 1 and self._state == "value":
                    self._string_role = "value"
                else:
                    self._string_role = "nested"
                continue

            if depth > 1:
                if value in {ord("{"), ord("[")}:
                    self._push(value)
                elif value in {ord("}"), ord("]")}:
                    self._pop(value)
                    if len(self._stack) == 1 and self._state == "value_nested":
                        self._active_key = None
                        self._state = "comma_or_end"
                continue

            if self._state == "colon":
                if value != ord(":"):
                    self._invalid = True
                    return
                self._state = "value"
                continue
            if self._state == "value":
                if value in {ord("{"), ord("[")}:
                    if self._active_key in self._STRING_FIELDS or self._active_key == "seq":
                        if self._active_key == "seq":
                            self._record_field("seq", None)
                        else:
                            self._record_field(self._active_key, None)
                        self._invalid = True
                    self._push(value)
                    self._state = "value_nested"
                    continue
                self._primitive_buf = bytearray([value])
                self._primitive_overflow = False
                continue
            if self._state == "key_or_end":
                if value == ord("}"):
                    self._pop(value)
                    self._state = "closed"
                    continue
                self._invalid = True
                return
            if self._state == "comma_or_end":
                if value == ord(","):
                    self._state = "key_or_end"
                    continue
                if value == ord("}"):
                    self._pop(value)
                    self._state = "closed"
                    continue
                self._invalid = True
                return

    def authoritative_seq(self, session_id: str, run_id: str) -> tuple[int | None, str]:
        seq = self._fields.get("seq")
        if self._seq_seen != 1 or isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
            return None, "replay_invalid_seq"
        if self._invalid or self._state != "closed" or self._stack or self._in_string:
            return None, "replay_invalid_envelope"
        if (
            self._fields.get("session_id") != str(session_id)
            or self._fields.get("run_id") != str(run_id)
            or self._fields.get("event_id") != f"{run_id}:{seq}"
        ):
            return None, "replay_invalid_identity"
        return seq, ""


def _read_bounded_physical_row(fh) -> tuple[bytes | None, _TopLevelEnvelopeScanner] | None:
    """Read and drain one JSONL row while retaining at most the hard-line ceiling."""
    scanner = _TopLevelEnvelopeScanner()
    retained = bytearray()
    total_bytes = 0
    while True:
        chunk = fh.readline(_SESSION_REPLAY_READ_CHUNK_BYTES)
        if not chunk:
            break
        total_bytes += len(chunk)
        scanner.feed(chunk)
        remaining = _LEGACY_TERMINAL_RECOVERY_MAX_BYTES + 1 - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
        if chunk.endswith(b"\n"):
            break
    if total_bytes == 0:
        return None
    if total_bytes > _LEGACY_TERMINAL_RECOVERY_MAX_BYTES:
        return None, scanner
    return bytes(retained), scanner


def _replay_limit_result(
    session_id: str,
    run_id: str,
    events: list[dict],
    malformed: list[dict],
    *,
    line_no: int,
    reason: str,
    next_after_seq: int,
) -> dict:
    return {
        "session_id": str(session_id), "run_id": str(run_id), "events": events,
        "malformed": [*malformed, {"line": line_no, "reason": reason}],
        "complete": False, "limit_reason": reason, "next_after_seq": next_after_seq,
    }


def read_run_events(
    session_id: str,
    run_id: str,
    *,
    after_seq: int | None = None,
    max_seq: int | None = None,
    session_dir: Path | None = None,
    max_bytes: int | None = None,
    max_rows: int | None = None,
) -> dict:
    path = _run_path(session_id, run_id, session_dir=session_dir)
    if max_bytes is None and max_rows is None:
        events, malformed = _read_jsonl(path)
        if after_seq is not None:
            events = [event for event in events if int(event.get("seq") or 0) > int(after_seq)]
        if max_seq is not None:
            events = [event for event in events if int(event.get("seq") or 0) <= int(max_seq)]
        return {
            "session_id": str(session_id),
            "run_id": str(run_id),
            "events": events,
            "malformed": malformed,
        }

    row_cap = None if max_rows is None else int(max_rows)
    if row_cap is not None and row_cap < 1:
        raise ValueError("max_rows must be at least 1")
    byte_cap = None if max_bytes is None else max(0, int(max_bytes))
    floor = int(after_seq) if after_seq is not None else None
    ceiling = int(max_seq) if max_seq is not None else None
    events: list[dict] = []
    malformed: list[dict] = []
    emitted_bytes = 0
    next_after_seq = floor or 0
    last_physical_seq = 0
    try:
        fh = path.open("rb")
    except FileNotFoundError:
        return {
            "session_id": str(session_id), "run_id": str(run_id), "events": events,
            "malformed": malformed, "complete": True, "limit_reason": None,
            "next_after_seq": next_after_seq,
        }
    with fh:
        line_no = 0
        while True:
            row = _read_bounded_physical_row(fh)
            if row is None:
                break
            line_no += 1
            raw_bytes, envelope = row
            if raw_bytes is not None and not raw_bytes.strip():
                continue
            seq, identity_error = envelope.authoritative_seq(
                str(session_id), str(run_id),
            )
            if seq is None:
                malformed.append({"line": line_no, "reason": identity_error})
                continue
            event = None
            if raw_bytes is not None:
                try:
                    event = json.loads(raw_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    malformed.append({"line": line_no, "raw": ""})
                    continue
                if not isinstance(event, dict):
                    malformed.append({"line": line_no, "raw": ""})
                    continue
            if seq <= last_physical_seq:
                malformed.append({"line": line_no, "reason": "replay_invalid_seq_order"})
                continue
            last_physical_seq = seq
            if floor is not None and seq <= floor:
                continue
            if ceiling is not None and seq > ceiling:
                continue
            if row_cap is not None and len(events) >= row_cap:
                return _replay_limit_result(
                    str(session_id), str(run_id), events, malformed,
                    line_no=line_no, reason="replay_limit_rows",
                    next_after_seq=next_after_seq,
                )
            if raw_bytes is None:
                # This row cannot be materialized within the hard-line ceiling.
                # Its exact top-level identity has been consumed and the complete
                # physical line drained, so the next page can safely advance.
                return _replay_limit_result(
                    str(session_id), str(run_id), events, malformed,
                    line_no=line_no, reason="replay_limit_bytes",
                    next_after_seq=seq,
                )
            assert event is not None
            event_size = _serialized_event_size(event)
            if byte_cap is not None and emitted_bytes + event_size > byte_cap:
                if event_size <= byte_cap:
                    # The candidate fits a fresh page. It has not been emitted or
                    # dispositioned, so leave the cursor on the last delivered row.
                    return _replay_limit_result(
                        str(session_id), str(run_id), events, malformed,
                        line_no=line_no, reason="replay_limit_bytes",
                        next_after_seq=next_after_seq,
                    )
                recovered_event = (
                    _recover_legacy_overcap_terminal_event(
                        event,
                        session_id=str(session_id),
                        run_id=str(run_id),
                        max_seq=ceiling,
                    )
                    if event.get("terminal") is True
                    else None
                )
                recovered_size = (
                    _serialized_event_size(recovered_event)
                    if recovered_event is not None
                    else None
                )
                if recovered_event is None or recovered_size > byte_cap:
                    # This row cannot fit even on an empty page. Consume its exact
                    # sequence as a non-materializable disposition to ensure progress.
                    return _replay_limit_result(
                        str(session_id), str(run_id), events, malformed,
                        line_no=line_no, reason="replay_limit_bytes",
                        next_after_seq=seq,
                    )
                if emitted_bytes + recovered_size > byte_cap:
                    return _replay_limit_result(
                        str(session_id), str(run_id), events, malformed,
                        line_no=line_no, reason="replay_limit_bytes",
                        next_after_seq=next_after_seq,
                    )
                event = recovered_event
                event_size = recovered_size
            events.append(event)
            emitted_bytes += event_size
            next_after_seq = seq
    return {
        "session_id": str(session_id), "run_id": str(run_id), "events": events,
        "malformed": malformed, "complete": True, "limit_reason": None,
        "next_after_seq": next_after_seq,
    }


def _summary_from_events(session_id: str, run_id: str, events: Iterable[dict]) -> dict:
    ordered = [event for event in events if isinstance(event, dict)]
    last = ordered[-1] if ordered else None
    terminal_events = [event for event in ordered if event.get("terminal")]
    terminal = next(
        (event for event in reversed(terminal_events) if event.get("event") != "stream_end"),
        terminal_events[-1] if terminal_events else None,
    )
    status = terminal.get("terminal_state") if terminal else ("running" if ordered else "unknown")
    return {
        "session_id": str(session_id),
        "run_id": str(run_id),
        "stream_id": str(run_id),
        "event_count": len(ordered),
        "last_seq": int((last or {}).get("seq") or 0),
        "last_event_id": (last or {}).get("event_id"),
        "terminal": bool(terminal),
        "terminal_state": status,
        "last_event": (last or {}).get("event"),
    }


def latest_run_summary(session_id: str, run_id: str, *, session_dir: Path | None = None) -> dict:
    path = _run_path(session_id, run_id, session_dir=session_dir)
    cached = _get_cached_summary(path)
    if cached is not None:
        return cached
    pre_read_signature = _summary_cache_signature(path)
    events, _malformed = _read_jsonl(path)
    summary = _summary_from_events(session_id, run_id, events)
    _cache_summary(path, summary, expected_signature=pre_read_signature)
    return summary


def session_journal_fingerprint(session_id: str, *, session_dir: Path | None = None) -> tuple[int, float, int]:
    """Cheap, bounded fingerprint of a session's run journal: (file_count, max_mtime, total_size).

    Reads only directory + per-file stat metadata (never parses journal bodies), so it stays
    O(runs) and cannot be tipped over by a large ``done`` row. Used to detect that the journal
    advanced during an idle live-subscribe wait — a run that starts AND finishes inside a single
    keepalive tick leaves the journal changed but never materializes a live in-memory stream, so a
    no-cursor idle subscriber would otherwise miss it until a manual refresh. Returns (0, 0.0, 0)
    when the session has no journal yet. Invalid ids resolve to the empty fingerprint rather than
    raising so callers can probe unconditionally.
    """
    try:
        sid = _validate_id(session_id, "session_id")
    except ValueError:
        return (0, 0.0, 0)
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    session_root = root / RUN_JOURNAL_DIR_NAME / sid
    if not session_root.exists():
        return (0, 0.0, 0)
    count = 0
    max_mtime = 0.0
    total_size = 0
    for path in session_root.glob("*.jsonl"):
        try:
            st = path.stat()
        except OSError:
            continue
        count += 1
        total_size += st.st_size
        if st.st_mtime > max_mtime:
            max_mtime = st.st_mtime
    return (count, max_mtime, total_size)


def find_run_summary(run_id: str, *, session_dir: Path | None = None) -> dict | None:
    rid = _validate_id(run_id, "run_id")
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    journal_root = root / RUN_JOURNAL_DIR_NAME
    for path in journal_root.glob(f"*/{rid}.jsonl"):
        session_id = path.parent.name
        summary = _get_cached_summary(path)
        if summary is None:
            pre_read_signature = _summary_cache_signature(path)
            events, _malformed = _read_jsonl(path)
            summary = _summary_from_events(session_id, rid, events)
            _cache_summary(path, summary, expected_signature=pre_read_signature)
        summary["path"] = str(path)
        return summary
    return None


def read_session_run_events(
    session_id: str,
    *,
    after_event_id: str | None = None,
    session_dir: Path | None = None,
    max_bytes: int = _SESSION_REPLAY_MAX_BYTES,
    max_rows: int = _SESSION_REPLAY_MAX_ROWS,
) -> dict:
    """Replay durable run-journal rows for one session after an opaque cursor."""
    sid = _validate_id(session_id, "session_id")
    cursor_run_id, cursor_seq = _parse_run_journal_event_id(after_event_id)
    raw_cursor = str(after_event_id or "").strip()
    if raw_cursor and cursor_run_id is not None:
        try:
            cursor_run_id = _validate_id(cursor_run_id, "run_id")
        except ValueError:
            cursor_seq = None
    if raw_cursor:
        try:
            if int(raw_cursor.rsplit(":", 1)[-1]) < 0:
                cursor_seq = None
        except (TypeError, ValueError):
            pass
    if raw_cursor and (cursor_run_id is None or cursor_seq is None or cursor_seq <= 0):
        return {
            "session_id": sid,
            "cursor_run_id": cursor_run_id,
            "cursor_seq": cursor_seq,
            "status": "cursor_invalid",
            "events": [],
        }
    if not raw_cursor:
        return {
            "session_id": sid,
            "cursor_run_id": None,
            "cursor_seq": None,
            "status": "ok",
            "events": [],
        }
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    session_root = root / RUN_JOURNAL_DIR_NAME / sid
    runs: list[tuple[float, str, list[dict]]] = []
    retained_rows = 0
    retained_bytes = 0
    for path in sorted(session_root.glob("*.jsonl")) if session_root.exists() else []:
        run_id = path.stem
        try:
            run_id = _validate_id(run_id, "run_id")
        except ValueError:
            continue
        events: list[dict] = []
        expected_seq = 1
        try:
            for _line_no, raw, total_bytes in _iter_bounded_raw_jsonl_lines(
                path,
                max_bytes=max_bytes,
                retained_bytes=retained_bytes,
            ):
                retained_bytes = total_bytes
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw.decode("utf-8"))
                    seq = int(event.get("seq")) if isinstance(event, dict) else 0
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                    return {"session_id": sid, "cursor_run_id": cursor_run_id, "cursor_seq": cursor_seq, "status": "replay_malformed", "events": []}
                if (
                    seq != expected_seq
                    or event.get("event_id") != f"{run_id}:{seq}"
                    or event.get("run_id") != run_id
                    or event.get("session_id") != sid
                ):
                    return {"session_id": sid, "cursor_run_id": cursor_run_id, "cursor_seq": cursor_seq, "status": "replay_noncontiguous", "events": []}
                expected_seq += 1
                retained_rows += 1
                if retained_rows > max_rows:
                    return {"session_id": sid, "cursor_run_id": cursor_run_id, "cursor_seq": cursor_seq, "status": "replay_limit_rows", "events": []}
                events.append(event)
        except FileNotFoundError:
            continue
        except ValueError as exc:
            if str(exc) == "replay_limit_bytes":
                return {"session_id": sid, "cursor_run_id": cursor_run_id, "cursor_seq": cursor_seq, "status": "replay_limit_bytes", "events": []}
            raise
        created_at = min((_event_created_at(event) for event in events), default=path.stat().st_mtime)
        runs.append((created_at, run_id, events))
    runs.sort(key=lambda run: (run[0], run[1]))
    cursor_index = next((index for index, (_created_at, run_id, _events) in enumerate(runs) if run_id == cursor_run_id), None)
    if cursor_index is None:
        foreign_paths = root.joinpath(RUN_JOURNAL_DIR_NAME).glob(f"*/{cursor_run_id}.jsonl") if cursor_run_id else []
        foreign_session_id = next((path.parent.name for path in foreign_paths if path.parent.name != sid), "")
        status = "cursor_run_missing"
        if foreign_session_id:
            status = "cursor_session_mismatch"
        return {
            "session_id": sid,
            "cursor_run_id": cursor_run_id,
            "cursor_seq": cursor_seq,
            "status": status,
            "events": [],
        }
    cursor_events = runs[cursor_index][2]
    if cursor_seq is None or cursor_seq > len(cursor_events):
        return {"session_id": sid, "cursor_run_id": cursor_run_id, "cursor_seq": cursor_seq, "status": "cursor_event_missing", "events": []}
    replay_events = [event for event in cursor_events if event["seq"] > cursor_seq]
    for _created_at, _run_id, events in runs[cursor_index + 1:]:
        replay_events.extend(events)
    return {
        "session_id": sid,
        "cursor_run_id": cursor_run_id,
        "cursor_seq": cursor_seq,
        "status": "ok",
        "events": replay_events,
    }


def delete_run_journal(session_id: str, *, session_dir: Path | None = None) -> bool:
    """Remove the entire per-session run-journal directory (``_run_journal/{sid}/``).

    The run journal stores one directory per session containing a ``{rid}.jsonl``
    file per run, so removing the session's directory clears every run's full
    request/response payloads. Invalid/empty ids and a missing directory are a
    no-op so callers can invoke this unconditionally on delete. Returns ``True``
    if a directory was removed, ``False`` otherwise.
    """
    import shutil

    sid = str(session_id or "").strip()
    # Reject path-traversal ids: the regex below permits dots, so a bare "." or
    # ".." would resolve `root / RUN_JOURNAL_DIR_NAME / sid` to the journal ROOT
    # (or its parent) and rmtree the wrong directory. The route call site only
    # passes real sids, but this is a public helper — guard it directly.
    if sid in (".", "..") or not sid or "/" in sid or "\\" in sid or not _SAFE_ID_RE.fullmatch(sid):
        return False
    root = Path(session_dir) if session_dir is not None else _default_session_dir()
    session_journal_dir = root / RUN_JOURNAL_DIR_NAME / sid
    if not session_journal_dir.exists():
        return False
    shutil.rmtree(session_journal_dir, ignore_errors=True)
    removed = not session_journal_dir.exists()
    # Evict any writer locks the removed runs left behind. `_lock_for` keys are
    # ``(str(path.parent), path.name, pid)`` and every run file for this session
    # lives directly under ``session_journal_dir``, so drop all keys whose parent
    # dir matches — pid-independent — to keep `_WRITER_LOCKS` from growing forever.
    # Guard on confirmed removal: `rmtree(ignore_errors=True)` can silently leave
    # the directory (locked files on Windows, permission transients). If the files
    # still exist their locks are still live — evicting them would hand a later
    # `_lock_for` caller a brand-new Lock, breaking mutual exclusion with a writer
    # still holding the old one.
    if removed:
        dir_key = str(session_journal_dir)
        with _WRITER_LOCKS_GUARD:
            for key in [k for k in _WRITER_LOCKS if k[0] == dir_key]:
                del _WRITER_LOCKS[key]
        # Drop cached next-seq entries for the removed runs too. Every run file
        # for this session lives directly under ``session_journal_dir``, so its
        # cache key's parent dir matches. Without this, a run re-created at the
        # same path would resume the stale cached seq instead of restarting at 1.
        # Hold ``_SEQ_CACHE_LOCK`` — the SAME mutex ``_reserve_next_seq``/
        # ``_note_assigned_seq`` take — so a concurrent append on another path
        # cannot mutate the dict mid-iteration (``dictionary changed size``).
        with _SEQ_CACHE_LOCK:
            for cache_key in [entry for entry in _SEQ_CACHE if str(Path(entry).parent) == dir_key]:
                del _SEQ_CACHE[cache_key]
        with _SUMMARY_CACHE_LOCK:
            for cache_key in [entry for entry in _SUMMARY_CACHE if str(Path(entry).parent) == dir_key]:
                del _SUMMARY_CACHE[cache_key]
    return removed


def stale_interrupted_event(session_id: str, run_id: str, *, after_seq: int | None = None) -> dict | None:
    summary = latest_run_summary(session_id, run_id)
    if summary.get("terminal") or not summary.get("event_count"):
        return None
    seq = int(summary.get("last_seq") or 0) + 1
    if after_seq is not None and seq <= int(after_seq):
        return None
    payload = {
        "type": "interrupted",
        "recovery_control": True,
        "message": "The live worker stopped before this run finished.",
        "hint": "The transcript was restored to the last journaled event. Start a new turn if you still need the task to continue.",
        "session_id": session_id,
        "stream_id": run_id,
        "journal_last_seq": summary.get("last_seq"),
    }
    return {
        "version": 1,
        "event_id": f"{run_id}:{seq}",
        "seq": seq,
        "run_id": run_id,
        "session_id": session_id,
        "event": "apperror",
        "type": "apperror",
        "created_at": time.time(),
        "terminal": True,
        "terminal_state": "lost-worker-bookkeeping",
        "payload": payload,
        "synthetic": True,
    }
