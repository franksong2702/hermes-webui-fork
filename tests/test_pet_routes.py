import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pet_attention_uses_latest_visible_assistant_segment(monkeypatch):
    import api.pet_routes as pet_routes
    import api.run_journal

    def fake_read_run_events(session_id, run_id):
        assert session_id == "sid-1"
        assert run_id == "run-1"
        return {
            "events": [
                {"event": "token", "payload": {"text": "贡献规则确认："}},
                {"event": "token", "payload": {"text": "PR 必须六段 body。"}},
                {
                    "event": "interim_assistant",
                    "payload": {
                        "text": "贡献规则确认：PR 必须六段 body。",
                        "already_streamed": True,
                    },
                },
                {"event": "tool", "payload": {"name": "shell"}},
                {"event": "token", "payload": {"text": "验证结果对了："}},
                {"event": "token", "payload": {"text": "同一个 active run 现在返回的是最新 interim 人话，而不是旧消息。"}},
                {"event": "token", "payload": {"text": "第二句也保留，交给气泡两行省略。"}},
            ]
        }

    monkeypatch.setattr(api.run_journal, "read_run_events", fake_read_run_events)

    text = pet_routes._pet_latest_visible_assistant_process_text({
        "session_id": "sid-1",
        "active_stream_id": "run-1",
        "is_streaming": True,
    })

    assert text == "验证结果对了：同一个 active run 现在返回的是最新 interim 人话，而不是旧消息。第二句也保留，交给气泡两行省略。"
    assert "贡献规则确认" not in text


def test_pet_attention_stale_stream_cleanup_is_display_only(monkeypatch):
    import api.pet_routes as pet_routes

    class FakeStreamLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    rows = [
        {
            "session_id": "sid-stale",
            "active_stream_id": "missing-run",
            "is_streaming": False,
            "pending_user_message": "queued",
            "has_pending_user_message": True,
        }
    ]
    monkeypatch.setattr(pet_routes, "STREAMS", {})
    monkeypatch.setattr(pet_routes, "STREAMS_LOCK", FakeStreamLock())

    display_rows = pet_routes._display_rows_without_stale_pet_streams(rows)

    assert display_rows == [
        {
            "session_id": "sid-stale",
            "active_stream_id": None,
            "is_streaming": False,
            "pending_user_message": None,
            "has_pending_user_message": False,
        }
    ]
    assert rows[0]["active_stream_id"] == "missing-run"
    assert rows[0]["pending_user_message"] == "queued"


def test_pet_message_text_ignores_user_messages_for_final_bubbles(monkeypatch):
    import api.pet_routes as pet_routes

    class FakeSession:
        messages = [
            {"role": "user", "content": "用户刚刚发的消息不应该出现"},
            {"role": "assistant", "content": "Cerebras System 是什么。第二句也应该交给气泡省略。"},
        ]

    monkeypatch.setattr(pet_routes.Session, "load", staticmethod(lambda sid: FakeSession()))

    text = pet_routes._pet_latest_assistant_final_text("sid-1")
    assert text.startswith("Cerebras System 是什么")
    assert "用户刚刚发的消息" not in text


def test_pet_attention_marks_pending_approval_action_required(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(
        pet_routes,
        "_pet_pending_approval",
        lambda sid: ({"description": "Dangerous command detected", "command": "rm -rf /tmp/test"}, 2),
    )
    monkeypatch.setattr(pet_routes, "_pet_pending_clarify", lambda sid: (None, 0))

    item = pet_routes._pet_attention_session({
        "session_id": "sid-approval",
        "display_title": "Needs approval",
        "is_streaming": True,
        "message_count": 4,
    })

    assert item["action_required"] is True
    assert item["action_required_type"] == "approval"
    assert item["action_required_count"] == 2
    assert item["process_text"].startswith("需要批准：")
    assert "Dangerous command detected" in item["process_text"]


def test_pet_attention_truncates_long_action_required_copy(monkeypatch):
    import api.pet_routes as pet_routes

    long_command = "run " + ("very-long-argument-" * 30)
    monkeypatch.setattr(
        pet_routes,
        "_pet_pending_approval",
        lambda sid: ({"description": "Dangerous command detected", "command": long_command}, 1),
    )
    monkeypatch.setattr(pet_routes, "_pet_pending_clarify", lambda sid: (None, 0))

    item = pet_routes._pet_attention_session({
        "session_id": "sid-long-approval",
        "display_title": "Needs approval",
        "is_streaming": True,
        "message_count": 4,
    })

    assert item["process_text"].startswith("需要批准：Dangerous command detected:")
    assert item["process_text"].endswith("...")
    assert len(item["process_text"]) <= pet_routes._PET_ACTION_TEXT_MAX_CHARS + len("需要批准：")


def test_pet_attention_marks_pending_clarify_action_required(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(pet_routes, "_pet_pending_approval", lambda sid: (None, 0))
    monkeypatch.setattr(
        pet_routes,
        "_pet_pending_clarify",
        lambda sid: ({"question": "Which environment should I deploy this to?"}, 1),
    )

    item = pet_routes._pet_attention_session({
        "session_id": "sid-clarify",
        "display_title": "Needs choice",
        "is_streaming": True,
        "message_count": 4,
    })

    assert item["action_required"] is True
    assert item["action_required_type"] == "clarify"
    assert item["process_text"] == "需要选择：Which environment should I deploy this to?"


def test_pet_attention_hides_external_sessions_when_setting_is_off(monkeypatch):
    import api.pet_routes as pet_routes

    class Parsed:
        query = ""

    rows = [
        {
            "session_id": "webui-1",
            "display_title": "WebUI",
            "profile": "default",
            "message_count": 1,
        },
        {
            "session_id": "telegram-1",
            "display_title": "Telegram",
            "profile": "default",
            "message_count": 2,
            "is_cli_session": True,
            "session_source": "messaging",
            "source_tag": "telegram",
            "source_label": "Telegram",
        },
        {
            "session_id": "cli-1",
            "display_title": "CLI",
            "profile": "default",
            "message_count": 3,
            "is_cli_session": True,
            "session_source": "cli",
            "source_tag": "cli",
            "source_label": "CLI",
        },
    ]
    monkeypatch.setattr(pet_routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(pet_routes, "load_settings", lambda: {"show_cli_sessions": False})

    filtered, _profile = pet_routes._filter_pet_attention_rows(rows, Parsed())

    assert [row["session_id"] for row in filtered] == ["webui-1"]


def test_pet_attention_respects_external_visibility_setting(monkeypatch):
    import api.pet_routes as pet_routes

    class Parsed:
        query = ""

    rows = [
        {
            "session_id": "telegram-1",
            "display_title": "Telegram",
            "profile": "default",
            "message_count": 2,
            "actual_user_message_count": 2,
            "is_cli_session": True,
            "session_source": "messaging",
            "source_tag": "telegram",
            "source_label": "Telegram",
        },
    ]
    monkeypatch.setattr(pet_routes, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(pet_routes, "load_settings", lambda: {"show_cli_sessions": True})

    filtered, _profile = pet_routes._filter_pet_attention_rows(rows, Parsed())

    assert [row["session_id"] for row in filtered] == ["telegram-1"]


def test_pet_bundled_skins_and_default_order():
    import api.pet_routes as pet_routes

    courier_manifest = json.loads((ROOT / "static" / "pets" / "courier" / "pet.json").read_text(encoding="utf-8"))
    keeper_manifest = json.loads((ROOT / "static" / "pets" / "keeper" / "pet.json").read_text(encoding="utf-8"))
    shiba_manifest = json.loads((ROOT / "static" / "pets" / "shiba" / "pet.json").read_text(encoding="utf-8"))
    skins = pet_routes._available_pet_skins()

    assert courier_manifest["id"] == "courier"
    assert courier_manifest["displayName"] == "Courier Bot"
    assert keeper_manifest["id"] == "keeper"
    assert keeper_manifest["displayName"] == "May"
    assert shiba_manifest["id"] == "shiba"
    assert shiba_manifest["displayName"] == "shiba"
    assert (ROOT / "static" / "pets" / "courier" / "spritesheet.webp").stat().st_size > 100_000
    assert (ROOT / "static" / "pets" / "keeper" / "spritesheet.webp").stat().st_size > 100_000
    assert (ROOT / "static" / "pets" / "shiba" / "spritesheet.webp").stat().st_size > 100_000
    assert skins[0]["id"] == "keeper"
    assert {skin["id"] for skin in skins} >= {"courier", "keeper", "shiba"}
    assert pet_routes.DEFAULT_PET_SKIN_ID == "keeper"


def test_pet_routes_are_owned_by_pet_module_and_thin_dispatched():
    routes = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    pet_routes = (ROOT / "api" / "pet_routes.py").read_text(encoding="utf-8")

    assert "from api import pet_routes" in routes
    assert "pet_routes.handle_get(handler, parsed)" in routes
    assert "pet_routes.handle_post(handler, parsed, body)" in routes
    assert "def _handle_pet_attention" not in routes
    assert "def _available_pet_skins" not in routes
    assert "def _handle_pet_open_session" not in routes
    assert "def _handle_pet_attention" in pet_routes
    assert "def _available_pet_skins" in pet_routes
    assert "def _handle_pet_navigation" in pet_routes
    assert "def _handle_pet_open_session" in pet_routes
    assert "def _handle_pet_status" in pet_routes
    assert "def _handle_pet_launch" in pet_routes
    assert "def _handle_pet_close" in pet_routes
    assert "DEFAULT_PET_SKIN_ID = \"keeper\"" in pet_routes
    assert "process_text = \"正在思考\"" in pet_routes
    assert "message.get(\"reasoning\")" not in pet_routes


def test_pet_navigation_command_is_queued(monkeypatch):
    import api.pet_routes as pet_routes

    class Handler:
        headers = {"Host": "127.0.0.1:8788"}

    monkeypatch.setattr(pet_routes, "get_session", lambda sid, metadata_only=False: object())
    pet_routes._PET_NAVIGATION_COMMANDS.clear()

    command = pet_routes._queue_pet_session_navigation(
        Handler(),
        {"session_id": "sid-1", "draft": "继续", "autosend": True},
    )

    assert command["session_id"] == "sid-1"
    assert command["draft"] == "继续"
    assert command["autosend"] is True
    assert command["url"] == "http://127.0.0.1:8788/session/sid-1?draft=%E7%BB%A7%E7%BB%AD"
    assert pet_routes._PET_NAVIGATION_COMMANDS == [command]


def test_pet_navigation_commands_are_fifo(monkeypatch):
    import api.pet_routes as pet_routes

    class Handler:
        headers = {"Host": "127.0.0.1:8787"}

    monkeypatch.setattr(pet_routes, "get_session", lambda sid, metadata_only=False: object())
    pet_routes._PET_NAVIGATION_COMMANDS.clear()

    first = pet_routes._queue_pet_session_navigation(Handler(), {"session_id": "sid-1"})
    second = pet_routes._queue_pet_session_navigation(Handler(), {"session_id": "sid-2"})

    with pet_routes._PET_NAVIGATION_LOCK:
        assert pet_routes._next_pet_navigation_command_locked("")["id"] == first["id"]
        assert pet_routes._next_pet_navigation_command_locked(first["id"])["id"] == second["id"]
        assert pet_routes._next_pet_navigation_command_locked(second["id"]) == {}


def test_pet_open_session_queues_url_and_focuses_existing_webui_tab(monkeypatch):
    import api.pet_routes as pet_routes

    class Handler:
        headers = {"Host": "127.0.0.1:8787"}

    monkeypatch.setattr(pet_routes, "get_session", lambda sid, metadata_only=False: object())
    focused = []
    monkeypatch.setattr(pet_routes.sys, "platform", "darwin")
    monkeypatch.setattr(pet_routes, "_focus_existing_pet_browser_tab", lambda url: focused.append(url) or True)
    pet_routes._PET_NAVIGATION_COMMANDS.clear()

    command = pet_routes._queue_and_focus_pet_session_navigation(
        Handler(),
        {"session_id": "sid-1", "draft": "继续", "autosend": True},
    )

    assert command["session_id"] == "sid-1"
    assert command["focused"] is True
    assert focused == ["http://127.0.0.1:8787/session/sid-1?draft=%E7%BB%A7%E7%BB%AD"]


def test_pet_open_url_rejects_host_and_scheme_injection():
    import api.pet_routes as pet_routes

    class Handler:
        headers = {"Host": "evil.example", "X-Forwarded-Proto": "javascript"}

    url = pet_routes._pet_open_url(Handler(), "sid-1", draft="hello", autosend=True)

    assert url == "http://127.0.0.1:8787/session/sid-1?draft=hello"
    assert "autosend" not in url
    try:
        pet_routes._pet_open_url(Handler(), "../sid")
    except ValueError as exc:
        assert str(exc) == "invalid session_id"
    else:
        raise AssertionError("path-like session ids must be rejected")


def test_pet_launch_env_follows_active_loopback_webui_url():
    import api.pet_routes as pet_routes

    class Handler:
        headers = {"Host": "localhost:8788", "X-Forwarded-Proto": "http"}

    scheme, host = pet_routes._pet_request_base(Handler())
    env = pet_routes._desktop_pet_launch_env(f"{scheme}://{host}")

    assert env["HERMES_DESKTOP_PET_BASE_URL"] == "http://localhost:8788"
    assert env["HERMES_WEBUI_BASE_URL"] == "http://localhost:8788"


def test_pet_skin_scan_rejects_manifest_path_traversal(monkeypatch, tmp_path):
    import api.pet_routes as pet_routes

    pets_root = tmp_path / "pets"
    bad_skin = pets_root / "bad"
    bad_skin.mkdir(parents=True)
    (bad_skin / "pet.json").write_text(
        json.dumps({"id": "bad", "displayName": "bad", "spritesheetPath": "../secret.webp"}),
        encoding="utf-8",
    )
    (pets_root / "secret.webp").write_bytes(b"not a skin")
    mismatched_id = pets_root / "mismatch"
    mismatched_id.mkdir()
    (mismatched_id / "pet.json").write_text(
        json.dumps({"id": "other", "displayName": "other", "spritesheetPath": "spritesheet.webp"}),
        encoding="utf-8",
    )
    (mismatched_id / "spritesheet.webp").write_bytes(b"skin")

    monkeypatch.setattr(pet_routes, "_pet_static_path", lambda *parts: tmp_path.joinpath(*parts))

    assert pet_routes._available_pet_skins() == []


def test_pet_routes_remain_behind_global_auth_gate():
    from api.auth import PUBLIC_PATHS

    assert "/pet" not in PUBLIC_PATHS
    assert "/api/pet/attention" not in PUBLIC_PATHS
    assert "/api/pet/skins" not in PUBLIC_PATHS
    assert "/api/pet/navigation" not in PUBLIC_PATHS
    assert "/api/pet/status" not in PUBLIC_PATHS
    assert "/api/pet/install" not in PUBLIC_PATHS
    assert "/api/pet/launch" not in PUBLIC_PATHS
    assert "/api/pet/close" not in PUBLIC_PATHS
    assert "/api/pet/open_session" not in PUBLIC_PATHS


def test_pet_launch_is_loopback_only():
    import api.pet_routes as pet_routes

    class LocalHandler:
        client_address = ("127.0.0.1", 12345)

    class RemoteHandler:
        client_address = ("192.168.10.99", 12345)

    assert pet_routes._pet_client_is_loopback(LocalHandler()) is True
    assert pet_routes._pet_client_is_loopback(RemoteHandler()) is False


def test_pet_open_session_is_loopback_only(monkeypatch):
    import api.pet_routes as pet_routes

    class RemoteHandler:
        client_address = ("192.168.10.99", 12345)
        calls = []

    queued = []
    monkeypatch.setattr(pet_routes, "_queue_and_focus_pet_session_navigation", lambda handler, body: queued.append(body))
    monkeypatch.setattr(
        pet_routes,
        "bad",
        lambda handler, message, status=400: handler.calls.append((message, status)) or True,
    )

    handler = RemoteHandler()
    result = pet_routes._handle_pet_open_session(handler, {"session_id": "sid-1"})

    assert result is True
    assert queued == []
    assert handler.calls == [("desktop pet session navigation is only available from this machine", 403)]


def test_pet_launch_candidates_prefer_existing_shells(monkeypatch, tmp_path):
    import api.pet_routes as pet_routes

    root = tmp_path / "repo"
    binary = root / "desktop-pet" / "src-tauri" / "target" / "debug" / (
        "hermes-desktop-pet.exe" if pet_routes.os.name == "nt" else "hermes-desktop-pet"
    )
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    monkeypatch.setattr(pet_routes, "_repo_root", lambda: root)

    candidates = pet_routes._desktop_pet_launch_candidates()

    assert candidates
    assert candidates[0]["kind"] == "debug-binary"
    assert candidates[0]["argv"] == [str(binary)]


def test_pet_prepare_skips_build_when_shell_exists(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(pet_routes, "_desktop_pet_launch_candidates", lambda: [{"kind": "debug-binary"}])

    result = pet_routes._prepare_desktop_pet_shell()

    assert result["ok"] is True
    assert result["installed"] is True
    assert result["method"] == "debug-binary"


def test_pet_launch_is_single_instance(monkeypatch):
    import api.pet_routes as pet_routes

    def fail_candidates():
        raise AssertionError("existing desktop pet should be reused before launch candidates are inspected")

    monkeypatch.setattr(pet_routes, "_desktop_pet_process_running", lambda: True)
    monkeypatch.setattr(pet_routes, "_desktop_pet_launch_candidates", fail_candidates)

    result = pet_routes._launch_desktop_pet_process("http://127.0.0.1:8788")

    assert result["ok"] is True
    assert result["already_running"] is True
    assert result["method"] == "existing"
    assert result["base_url"] == "http://127.0.0.1:8788"


def test_pet_close_noops_when_not_running(monkeypatch):
    import api.pet_routes as pet_routes

    monkeypatch.setattr(pet_routes, "_desktop_pet_processes", lambda: [])

    result = pet_routes._close_desktop_pet_processes()

    assert result == {"ok": True, "closed": 0, "running": False}


def test_pet_close_on_windows_terminates_identified_pids_only(monkeypatch):
    import api.pet_routes as pet_routes

    calls = []

    class Result:
        returncode = 0
        stdout = "SUCCESS"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return Result()

    monkeypatch.setattr(pet_routes.os, "name", "nt")
    monkeypatch.setattr(
        pet_routes,
        "_desktop_pet_processes",
        lambda: [
            {"pid": 111, "command": "hermes-desktop-pet.exe", "verified_path": True},
            {"pid": 222, "command": "hermes-desktop-pet.exe", "verified_path": True},
        ],
    )
    monkeypatch.setattr(pet_routes, "_desktop_pet_process_running", lambda: False)
    monkeypatch.setattr(pet_routes.subprocess, "run", fake_run)

    result = pet_routes._close_desktop_pet_processes()

    assert result == {"ok": True, "closed": 2, "running": False, "error": ""}
    assert calls == [
        ["taskkill", "/PID", "111", "/T", "/F"],
        ["taskkill", "/PID", "222", "/T", "/F"],
    ]
    assert all("/IM" not in call for call in calls)


def test_pet_close_on_windows_refuses_unverified_tasklist_pids(monkeypatch):
    import api.pet_routes as pet_routes

    def fake_run(*args, **kwargs):
        raise AssertionError("taskkill should not run without a verified executable path")

    monkeypatch.setattr(pet_routes.os, "name", "nt")
    monkeypatch.setattr(
        pet_routes,
        "_desktop_pet_processes",
        lambda: [{"pid": 111, "command": "hermes-desktop-pet.exe", "verified_path": False}],
    )
    monkeypatch.setattr(pet_routes, "_desktop_pet_process_running", lambda: True)
    monkeypatch.setattr(pet_routes.subprocess, "run", fake_run)

    result = pet_routes._close_desktop_pet_processes()

    assert result == {
        "ok": False,
        "closed": 0,
        "running": True,
        "error": "No verified desktop pet process ids were available to close.",
    }


def test_pet_windows_process_detection_filters_to_known_executable_paths(monkeypatch):
    import api.pet_routes as pet_routes

    known_path = r"C:\Hermes\desktop-pet\src-tauri\target\release\hermes-desktop-pet.exe"
    other_path = r"C:\Other\hermes-desktop-pet.exe"

    class Result:
        returncode = 0
        stdout = json.dumps(
            [
                {
                    "ProcessId": 111,
                    "ExecutablePath": known_path,
                    "CommandLine": f'"{known_path}"',
                },
                {
                    "ProcessId": 222,
                    "ExecutablePath": other_path,
                    "CommandLine": f'"{other_path}"',
                },
            ]
        )
        stderr = ""

    monkeypatch.setattr(pet_routes.shutil, "which", lambda name: "powershell")
    monkeypatch.setattr(pet_routes.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(pet_routes, "_desktop_pet_known_process_paths", lambda: {known_path})

    processes = pet_routes._desktop_pet_windows_processes()

    assert processes == [{"pid": 111, "command": f'"{known_path}"', "verified_path": True}]


def test_pet_windows_tasklist_fallback_marks_pids_unverified(monkeypatch):
    import api.pet_routes as pet_routes

    class Result:
        returncode = 0
        stdout = '"hermes-desktop-pet.exe","111","Console","1","10,000 K"\n'
        stderr = ""

    monkeypatch.setattr(pet_routes.subprocess, "run", lambda *args, **kwargs: Result())

    processes = pet_routes._desktop_pet_windows_processes_from_tasklist()

    assert processes == [
        {
            "pid": 111,
            "command": '"hermes-desktop-pet.exe","111","Console","1","10,000 K"',
            "verified_path": False,
        }
    ]


def test_pet_process_detection_ignores_shell_commands_and_unowned_pet_binaries(monkeypatch):
    import api.pet_routes as pet_routes

    class Result:
        returncode = 0
        stdout = "123 zsh -lc find . -name 'Hermes Desktop Pet.app'\n456 /tmp/hermes-desktop-pet\n"

    monkeypatch.setattr(pet_routes.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(pet_routes.os, "getpid", lambda: 999)
    monkeypatch.setattr(pet_routes.os, "name", "posix")
    monkeypatch.setattr(pet_routes, "_desktop_pet_known_process_paths", lambda: {"/repo/desktop-pet/src-tauri/target/release/hermes-desktop-pet"})

    processes = pet_routes._desktop_pet_processes()

    assert processes == []


def test_pet_process_detection_accepts_owned_binary(monkeypatch):
    import api.pet_routes as pet_routes

    class Result:
        returncode = 0
        stdout = "456 /repo/desktop-pet/src-tauri/target/release/hermes-desktop-pet\n"

    monkeypatch.setattr(pet_routes.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(pet_routes.os, "getpid", lambda: 999)
    monkeypatch.setattr(pet_routes.os, "name", "posix")
    monkeypatch.setattr(pet_routes, "_desktop_pet_known_process_paths", lambda: {"/repo/desktop-pet/src-tauri/target/release/hermes-desktop-pet"})

    processes = pet_routes._desktop_pet_processes()

    assert processes == [{"pid": 456, "command": "/repo/desktop-pet/src-tauri/target/release/hermes-desktop-pet"}]


def test_pet_process_detection_accepts_app_bundle_executable(monkeypatch):
    import api.pet_routes as pet_routes

    class Result:
        returncode = 0
        stdout = "456 /Applications/Hermes Desktop Pet.app/Contents/MacOS/hermes-desktop-pet\n"

    monkeypatch.setattr(pet_routes.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(pet_routes.os, "getpid", lambda: 999)
    monkeypatch.setattr(pet_routes.os, "name", "posix")
    monkeypatch.setattr(
        pet_routes,
        "_desktop_pet_known_process_paths",
        lambda: {"/Applications/Hermes Desktop Pet.app/Contents/MacOS/hermes-desktop-pet"},
    )

    processes = pet_routes._desktop_pet_processes()

    assert processes == [{"pid": 456, "command": "/Applications/Hermes Desktop Pet.app/Contents/MacOS/hermes-desktop-pet"}]
