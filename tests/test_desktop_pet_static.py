import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def bytes_for(path: str) -> bytes:
    return (ROOT / path).read_bytes()


def _html_attr_values(source: str, attr: str) -> list[str]:
    values = []
    needle = f'{attr}="'
    start = 0
    while True:
        idx = source.find(needle, start)
        if idx < 0:
            return values
        value_start = idx + len(needle)
        value_end = source.find('"', value_start)
        assert value_end >= 0
        values.append(source[value_start:value_end])
        start = value_end + 1


def _assert_pet_url_resolves_to_root_path(base_url: str, value: str, expected_path: str):
    parsed = urlparse(urljoin(base_url, value))
    assert parsed.path == expected_path


def test_standalone_pet_page_assets_and_apis_are_wired():
    pet_html = read("static/desktop_pet/index.html")
    bubbles_html = read("static/desktop_pet/bubbles.html")
    pet_js = read("static/desktop_pet/pet.js")
    bubbles_js = read("static/desktop_pet/bubbles.js")
    css = read("static/desktop_pet/pet.css")
    sw = read("static/sw.js")
    routes = read("api/pet_routes.py")
    removed_pet_script = "static/" + "floating" + "_pet.js"

    assert "/static/desktop_pet/pet.css?v=__WEBUI_VERSION__" in pet_html
    assert "/static/desktop_pet/pet.js?v=__WEBUI_VERSION__" in pet_html
    assert "/static/i18n.js?v=__WEBUI_VERSION__" in pet_html
    assert 'body class="pet-body"' in pet_html
    assert 'id="petStage"' in pet_html
    assert 'id="petBadge"' in pet_html
    assert 'id="petBubbles"' not in pet_html
    assert 'id="petInstall"' not in pet_html
    assert "__CSRF_TOKEN_JSON__" in pet_html
    assert "data-tauri-drag-region" in pet_html

    assert "/static/desktop_pet/bubbles.js?v=__WEBUI_VERSION__" in bubbles_html
    assert 'body class="pet-bubbles-body"' in bubbles_html
    assert 'id="petBubbles"' in bubbles_html
    assert 'id="petInstall" aria-live="polite" data-tauri-drag-region hidden' in bubbles_html
    assert 'id="petReadyToast"' in bubbles_html
    assert 'data-i18n-aria-label="desktop_pet_collapse_updates"' in bubbles_html
    assert 'id="petStage"' not in bubbles_html

    assert "def _handle_pet_page(handler, template: str = \"index.html\")" in routes
    assert 'if parsed.path == "/pet/bubbles":' in routes
    assert '_handle_pet_page(handler, "bubbles.html")' in routes
    assert 'return _handle_pet_page(handler, "bubbles.html")' not in routes

    assert "const FRAME_MS=520" in pet_js
    assert "const PET_WINDOW_FIXED={width:128,height:139}" in pet_js
    assert "PET_WINDOW_EXPANDED" not in pet_js
    assert "PET_WINDOW_COMPACT" not in pet_js
    assert "PET_ANCHOR" not in pet_js
    assert "PET_COMPACT_ANCHOR" not in pet_js
    assert "function _resizePetWindow" not in pet_js
    assert ".setSize(" not in pet_js
    assert "fetch('/api/pet/attention'" in pet_js
    assert "fetch('/api/pet/skins'" in pet_js
    assert "tauri.event.emit('pet-layout-update'" in pet_js
    assert "tauri.event.emit('pet-attention-update'" in pet_js
    assert "tauri.event.emit('pet-context-menu',{skins:petSkins,activeSkinId:" in pet_js
    assert "menuLabels:_menuLabels()" in pet_js
    assert "tauri.event.listen('pet-skin-change'" in pet_js
    assert "const RESTART_POSITION_KEY='hermes-pet-restart-position'" in pet_js
    assert "async function _savePetRestartPosition" in pet_js
    assert "async function _restorePetRestartPosition" in pet_js
    assert "async function _restartPetInPlace" in pet_js
    assert "localStorage.setItem(RESTART_POSITION_KEY,JSON.stringify" in pet_js
    assert "localStorage.removeItem(RESTART_POSITION_KEY)" in pet_js
    assert "location.reload()" in pet_js
    assert "tauri.event.listen('pet-restart-requested'" in pet_js
    assert "const SKIN_MIGRATION_KEY='hermes-pet-skin-migration'" in pet_js
    assert "const DEFAULT_SKIN_ID='keeper'" in pet_js
    assert "const DEFAULT_SKIN_MIGRATION='keeper-default-v1'" in pet_js
    assert "let activeSkinId=_initialPetSkinId();" in pet_js
    assert "stored==='shiba'" in pet_js

    assert "const BUBBLE_WINDOW={width:320,height:164}" in bubbles_js
    assert "const INSTALL_WINDOW={width:320,height:300}" in bubbles_js
    assert "const TOAST_WINDOW={width:320,height:92}" in bubbles_js
    assert "function _syncBubbleWindow" in bubbles_js
    assert ".setSize(" in bubbles_js
    assert ".setPosition(" in bubbles_js
    assert ".hide()" in bubbles_js
    assert ".show()" in bubbles_js
    assert "tauri.event.listen('pet-layout-update'" in bubbles_js
    assert "tauri.event.listen('pet-attention-update'" in bubbles_js
    assert "fetch('/api/pet/attention'" in bubbles_js
    assert "fetch('/api/pet/skins'" in bubbles_js
    assert "fetch('/api/pet/open_session'" in bubbles_js
    assert "function _bubbleVerticalPlacement" in bubbles_js
    assert "const aboveFits=aboveY>=monitor.y+margin" in bubbles_js
    assert "const belowFits=belowY+size.height<=monitor.y+monitor.height-margin" in bubbles_js
    assert "const preferredVertical=preferredPlacement||(petCenterY<monitor.y+monitor.height/2?'below':'above')" in bubbles_js
    assert "function _modePreferredPlacement(mode)" in bubbles_js
    assert "return mode==='install'||mode==='toast'?'above':''" in bubbles_js
    assert "function _positionWindowSize(size,monitor)" in bubbles_js
    assert "return {width:size.width*scale,height:size.height*scale}" in bubbles_js
    assert "const windowSize=_positionWindowSize(size,monitor)" in bubbles_js
    assert "function _petCollisionRect(pet,margin)" in bubbles_js
    assert "function _rectsOverlap(a,b)" in bubbles_js
    assert "function _rectFitsMonitor(rect,monitor,margin)" in bubbles_js
    assert "function _verticalClearance(pet,monitor,margin)" in bubbles_js
    assert "return Math.max(margin,pet.height+margin*2)" in bubbles_js
    assert "function _bubbleCandidatePositions(pet,monitor,size,margin,mode)" in bubbles_js
    assert "const headX=pet.x+pet.width/2" in bubbles_js
    assert "{placement:'right',x:pet.x+pet.width+margin,y:centerY}" in bubbles_js
    assert "{placement:'left',x:pet.x-size.width-margin,y:centerY}" in bubbles_js
    assert "const blocked=_petCollisionRect(pet,margin)" in bubbles_js
    assert "!_rectsOverlap({...candidate,width:windowSize.width,height:windowSize.height},blocked)" in bubbles_js
    assert "_bubbleVerticalPlacement(pet,monitor,size,verticalGap,_modePreferredPlacement(mode))" in bubbles_js
    assert "monitor.x+monitor.width-windowSize.width-margin" in bubbles_js
    assert "monitor.y+monitor.height-windowSize.height-margin" in bubbles_js
    assert "const pos=_bubblePosition(latestPetLayout,size,mode)" in bubbles_js
    assert "if(!pos){" in bubbles_js
    assert "layout.align" not in bubbles_js
    assert "layout.placement" not in bubbles_js
    assert "Promise.all(startupPromises)" in bubbles_js
    assert "Promise.allSettled(startupPromises)" not in bubbles_js
    assert "const SKIN_MIGRATION_KEY='hermes-pet-skin-migration'" in bubbles_js
    assert "let activeSkinId=_initialPetSkinId();" in bubbles_js
    assert "stored==='shiba'" in bubbles_js
    assert "function _openSession(sid,status)" in bubbles_js
    assert "_openSessionInBrowser(sid).catch(err=>console.warn('Failed to open session from pet',err))" in bubbles_js
    assert "_openSession(card.dataset.sid,card.dataset.status);" in bubbles_js
    assert "{draft:text,autosend:true}" in bubbles_js
    assert "event.key==='Enter'&&!event.shiftKey&&!event.isComposing" in bubbles_js
    assert "const INSTALL_SEEN_KEY='hermes-pet-install-seen'" in bubbles_js
    assert "function _runFirstStartInstall" in bubbles_js
    assert 'if(installSprite) installSprite.style.backgroundImage=`url("${next.spritesheetUrl}")`' in bubbles_js
    assert "desktop_pet_ready_toast" in bubbles_js
    assert "_petT('desktop_pet_reply')" in bubbles_js
    assert "_petT('desktop_pet_action_required')" in bubbles_js
    assert "status==='action_required'" in bubbles_js
    assert "actionType:_clean(row.action_required_type)" in bubbles_js
    assert "data-action-type=" in bubbles_js
    assert "const symbol=type==='approval'?'!':'?';" in bubbles_js
    assert "_statusHtml(item)" in bubbles_js
    assert 'title="${_esc(item.text)}"' in bubbles_js
    assert "_petT('desktop_pet_sending')" in bubbles_js
    assert "_petT('desktop_pet_failed_to_send')" in bubbles_js
    assert "_petT('desktop_pet_latest')" in bubbles_js
    assert "'正在思考'" not in bubbles_js
    assert "'Ready for review'" not in bubbles_js
    assert "'Failed to send'" not in bubbles_js

    assert "background:transparent" in css
    assert ".pet-body{width:128px;height:139px;}" in css
    assert ".pet-shell{position:absolute;inset:0;width:128px;height:139px;margin:0;user-select:none;-webkit-user-select:none;pointer-events:none;}" in css
    assert ".pet-stage{position:absolute;right:0;bottom:0;width:128px;height:139px;border:0;padding:0;background:transparent;box-shadow:none;appearance:none;-webkit-appearance:none;cursor:grab;z-index:1;pointer-events:auto;}" in css
    assert ".pet-bubbles-body{width:100%;height:100%;}" in css
    assert ".pet-install{position:absolute;inset:0;z-index:20;display:flex;align-items:center;justify-content:center;background:transparent;backdrop-filter:none;}" in css
    assert ".pet-bubbles{position:absolute;left:10px;right:10px;bottom:36px;overflow:visible;z-index:3;pointer-events:auto;}" in css
    assert ".pet-ready-toast" in css
    assert ".pet-action-required" in css
    assert 'background:url("../pets/keeper/spritesheet.webp")' in css
    assert "-webkit-line-clamp:2" in css
    assert "max-height:34.8px" in css
    assert "overflow-wrap:anywhere" in css
    assert "word-break:break-word" in css
    assert ".pet-action-required.is-approval" in css
    assert ".pet-action-required.is-clarify" in css
    assert ".pet-viewport" in css
    assert "overflow-y:auto" in css
    assert ".pet-card[data-reply-open=\"1\"] .pet-reply-toggle{display:none;}" in css
    assert "pet-window-resizing" not in css

    assert "./static/desktop_pet/pet.css" in sw
    assert "./static/desktop_pet/pet.js" in sw
    assert "./static/desktop_pet/bubbles.js" in sw
    assert "./static/pets/courier/pet.json" in sw
    assert "./static/pets/courier/spritesheet.webp" in sw
    assert "./" + removed_pet_script not in sw
    assert "./static/pet_bridge.js" in sw


def test_pet_pages_use_root_absolute_assets_and_apis():
    pet_html = read("static/desktop_pet/index.html")
    bubbles_html = read("static/desktop_pet/bubbles.html")
    pet_js = read("static/desktop_pet/pet.js")
    bubbles_js = read("static/desktop_pet/bubbles.js")

    for value in _html_attr_values(pet_html, "href") + _html_attr_values(pet_html, "src"):
        if value.startswith("/static/"):
            _assert_pet_url_resolves_to_root_path("http://127.0.0.1:8787/pet", value, urlparse(value).path)
    for value in _html_attr_values(bubbles_html, "href") + _html_attr_values(bubbles_html, "src"):
        if value.startswith("/static/"):
            _assert_pet_url_resolves_to_root_path("http://127.0.0.1:8787/pet/bubbles", value, urlparse(value).path)

    assert "fetch('api/" not in pet_js
    assert "fetch('api/" not in bubbles_js
    assert "fetch('/api/pet/attention'" in pet_js
    assert "fetch('/api/pet/attention'" in bubbles_js
    assert "spritesheetUrl:'/static/pets/keeper/spritesheet.webp'" in pet_js
    assert "spritesheetUrl:'/static/pets/keeper/spritesheet.webp'" in bubbles_js


def test_main_webui_pet_bridge_is_narrow():
    index = read("static/index.html")
    panels = read("static/panels.js")
    css = read("static/style.css")
    bridge = read("static/pet_bridge.js")
    sessions = read("static/sessions.js")
    config = read("api/config.py")
    routes = read("api/pet_routes.py")
    ui = read("static/ui.js")
    removed_pet_script = "static/" + "floating" + "_pet.js"
    removed_pet_setting = "floating" + "_pet_enabled"

    assert 'static/pet_bridge.js?v=__WEBUI_VERSION__' in index
    assert 'id="settingsDesktopPetEnabled"' in index
    assert 'onchange="toggleDesktopPetFromAppearance(this.checked)"' in index
    assert 'id="btnOpenDesktopPet"' not in index
    assert 'data-i18n="desktop_pet_title"' in index
    assert '<span data-i18n="desktop_pet_title">Desktop pet (Beta)</span>' in index
    assert 'settings' + 'FloatingPetEnabled' not in index
    assert removed_pet_script not in index
    assert 'onclick="startDesktopPet()"' not in index
    assert 'id="desktopPetInlineStatus"' in index
    assert 'id="desktopPetSetup"' not in index
    assert 'id="desktopPetSetupLaunch"' not in index
    assert "closeDesktopPetSetup" not in index
    assert "desktop-pet-setup-overlay" not in css
    assert 'data-i18n="settings_desc_desktop_pet"' in index
    assert "async function startDesktopPet()" in panels
    assert "async function launchDesktopPet(options={})" in panels
    assert "async function closeDesktopPet()" in panels
    assert "async function toggleDesktopPetFromAppearance(enabled)" in panels
    assert "async function _waitForDesktopPetRunning" in panels
    assert "async function prepareDesktopPetInline()" in panels
    assert "const DESKTOP_PET_INSTALL_TIMEOUT_MS=600000" in panels
    assert "api('/api/pet/status',{method:'POST',body:'{}'})" in panels
    assert "openDesktopPetSetup" not in panels
    assert "api('/api/pet/install',{method:'POST',body:'{}',timeoutMs:DESKTOP_PET_INSTALL_TIMEOUT_MS})" in panels
    assert "api('/api/pet/launch',{method:'POST',body:'{}'})" in panels
    assert "api('/api/pet/close',{method:'POST',body:'{}'})" in panels

    assert "def _desktop_pet_shell_source_mtime()" in routes
    assert "def _desktop_pet_candidate_is_current" in routes
    assert "_desktop_pet_launch_candidates(include_stale=True)" in routes
    assert '"stale": bool(stale_candidates) and not bool(candidates),' in routes
    assert '"source_mtime": _desktop_pet_shell_source_mtime(),' in routes
    assert '"artifact_mtime":' in routes
    assert 'return _handle_pet_page(handler)' not in routes
    assert 'return _handle_pet_attention(handler, parsed)' not in routes
    assert 'return _handle_pet_status(handler, body)' not in routes
    assert 'return True' in routes[routes.index('def handle_get'):]
    assert 'return True' in routes[routes.index('def handle_post'):]
    assert "settings_desktop_pet_started" not in panels
    assert "settings_desktop_pet_already_running" not in panels
    assert "settings_desktop_pet_start_failed" in panels
    assert "settings_desktop_pet_setup_starting" in panels
    assert "showToast(t(key)" not in panels
    assert "window.open(" not in panels
    assert "def _queue_pet_session_navigation" in routes
    assert "def _queue_and_focus_pet_session_navigation" in routes
    assert "_focus_existing_pet_browser_tab(command.get(\"url\", \"\"))" in routes
    assert "_queue_and_open_pet_session_navigation" not in routes
    assert "_open_pet_session_in_existing_browser_window" not in routes
    assert "def _open_pet_session_url" not in routes
    assert 'subprocess.Popen(["open", url])' not in routes
    assert '["taskkill", "/PID", str(pid), "/T", "/F"]' in routes
    assert '["taskkill", "/IM", "hermes-desktop-pet.exe", "/F"]' not in routes
    assert "PET_NAVIGATION_LAST_KEY" in bridge
    assert "'/api/pet/navigation?since='" in bridge
    assert "window.__hermesApplyPetNavigationCommand(command)" in bridge
    assert "localStorage.setItem(PET_NAVIGATION_LAST_KEY,String(command.id))" in bridge
    assert "window.__hermesApplyPetNavigationCommand=async function(command)" in sessions
    assert "await loadSession(sid)" in sessions
    assert "await _applyExternalComposerDraft(sid, command.draft, !!command.autosend)" in sessions
    assert "url.searchParams.get('autosend')" not in sessions
    assert "void _applyExternalComposerDraft(targetSid||pathSid,draft,false)" in sessions
    assert "PET_NAVIGATION_LAST_KEY" not in sessions
    assert "function _pollPetNavigation" not in sessions
    assert removed_pet_setting not in panels
    assert "_set" + "FloatingPetEnabled" not in panels
    assert removed_pet_setting not in config
    assert "_sync" + "FloatingPetState" not in ui


def test_desktop_pet_tauri_shell_has_dynamic_webui_url_and_skin_menu():
    config = read("desktop-pet/src-tauri/tauri.conf.json")
    capability = read("desktop-pet/src-tauri/capabilities/pet-window-drag.json")
    package = read("desktop-pet/package.json")
    cargo = read("desktop-pet/src-tauri/Cargo.toml")
    main = read("desktop-pet/src-tauri/src/main.rs")
    readme = read("desktop-pet/README.md")
    dist_index = read("desktop-pet/dist/index.html")
    dist_bubbles = read("desktop-pet/dist/bubbles.html")

    assert '"devUrl"' not in config
    assert "http://127.0.0.1:8787/pet" not in config
    assert '"label": "pet"' in config
    assert '"url": "index.html"' in config
    assert '"width": 128' in config
    assert '"height": 139' in config
    assert '"label": "pet_bubbles"' in config
    assert '"url": "bubbles.html"' in config
    assert '"width": 320' in config
    assert '"height": 164' in config
    assert '"visible": false' in config
    assert '"decorations": false' in config
    assert '"transparent": true' in config
    assert '"alwaysOnTop": true' in config
    assert '"skipTaskbar": true' in config
    assert '"active": true' in config
    assert '"targets": ["app"]' in config
    assert '"icons/icon.icns"' in config
    assert '"icons/icon.ico"' in config
    assert '"withGlobalTauri": true' in config
    assert '"remote"' in capability
    assert '"windows": ["pet", "pet_bubbles"]' in capability
    assert '"http://127.0.0.1:*/*"' in capability
    assert '"http://localhost:*/*"' in capability
    assert '"core:window:allow-start-dragging"' in capability
    assert '"core:window:allow-current-monitor"' in capability
    assert '"core:window:allow-available-monitors"' in capability
    assert '"core:window:allow-outer-position"' in capability
    assert '"core:window:allow-outer-size"' in capability
    assert '"core:window:allow-set-size"' in capability
    assert '"core:window:allow-set-position"' in capability
    assert '"core:window:allow-show"' in capability
    assert '"core:window:allow-hide"' in capability
    assert '"core:event:allow-emit"' in capability
    assert '"core:event:allow-listen"' in capability
    assert 'serde = { version = "1", features = ["derive"] }' in cargo
    assert 'serde_json = "1"' in cargo
    assert 'const HERMES_DESKTOP_PET_BASE_URL_ENV: &str = "HERMES_DESKTOP_PET_BASE_URL";' in main
    assert 'const FALLBACK_WEBUI_BASE_URL: &str = "http://127.0.0.1:8787";' in main
    assert "fn normalize_loopback_base_url" in main
    assert "fn navigate_pet_windows" in main
    assert 'window.navigate(pet_page_url(&base, "/pet"))' in main
    assert 'window.navigate(pet_page_url(&base, "/pet/bubbles"))' in main
    assert 'host != "localhost" && host != "::1" && !host.starts_with("127.")' in main
    assert 'url.set_host(Some("localhost")).ok()?' in main
    assert 'const CLOSE_PET_MENU_ID: &str = "close_pet";' in main
    assert 'const RESTART_PET_MENU_ID: &str = "restart_pet";' in main
    assert 'const PET_CONTEXT_MENU_EVENT: &str = "pet-context-menu";' in main
    assert 'const PET_SKIN_CHANGE_EVENT: &str = "pet-skin-change";' in main
    assert "struct PetContextMenuLabels" in main
    assert "fn valid_skin_id" in main
    assert "filter_map(sanitize_skin)" in main
    assert ".filter(|id| valid_skin_id(id))" in main
    assert "if !valid_skin_id(skin_id)" in main
    assert "labels.and_then(|item| item.switch_skin.as_ref())" in main
    assert "labels.and_then(|item| item.restart_pet.as_ref())" in main
    assert "labels.and_then(|item| item.close_pet.as_ref())" in main
    assert '"Switch skin"' in main
    assert '"Restart pet"' in main
    assert '"Close pet"' in main
    assert 'SubmenuBuilder::new(&menu_handle, switch_skin_label)' in main
    assert ".text(RESTART_PET_MENU_ID, restart_pet_label)" in main
    assert ".text(CLOSE_PET_MENU_ID, close_pet_label)" in main
    assert "切换皮肤" not in main
    assert "重启宠物" not in main
    assert "关闭宠物" not in main
    assert "window.popup_menu(&menu)" in main
    assert 'const PET_RESTART_REQUESTED_EVENT: &str = "pet-restart-requested";' in main
    assert "app.emit_to(\"pet\", PET_SKIN_CHANGE_EVENT, skin_id.clone())" in main
    assert "app.emit_to(\"pet_bubbles\", PET_SKIN_CHANGE_EVENT, skin_id)" in main
    assert "RESTART_PET_MENU_ID => {" in main
    assert "let _ = app.emit_to(\"pet\", PET_RESTART_REQUESTED_EVENT, ());" in main
    assert "app.request_restart()" not in main
    assert "CLOSE_PET_MENU_ID => app.exit(0)" in main
    assert 'active_skin_id: Some("keeper".into())' in main
    assert 'unwrap_or("keeper")' in main
    assert '"@tauri-apps/cli"' in package
    assert "HERMES_WEBUI_PORT=8788 ./start.sh" in readme
    assert "HERMES_DESKTOP_PET_BASE_URL" in readme
    assert "python3 ../server.py" not in readme
    assert "location.replace('http://127.0.0.1:8787/pet')" not in dist_index
    assert "http://127.0.0.1:8787" not in dist_bubbles


def test_desktop_pet_uses_webui_app_icon_assets():
    source_hash = hashlib.sha256(bytes_for("static/favicon-512.png")).hexdigest()
    pet_hash = hashlib.sha256(bytes_for("desktop-pet/src-tauri/icons/icon.png")).hexdigest()

    assert pet_hash == source_hash
    assert len(bytes_for("desktop-pet/src-tauri/icons/icon.icns")) > 100_000
    assert len(bytes_for("desktop-pet/src-tauri/icons/32x32.png")) > 1_000
    assert len(bytes_for("desktop-pet/src-tauri/icons/128x128.png")) > 5_000
    assert len(bytes_for("desktop-pet/src-tauri/icons/128x128@2x.png")) > 10_000


def test_desktop_pet_i18n_keys_exist_in_all_locales():
    i18n = read("static/i18n.js")
    keys = [
        "desktop_pet_title:",
        "desktop_pet_shell_label:",
        "desktop_pet_collapse_updates:",
        "desktop_pet_expand_updates:",
        "desktop_pet_thinking:",
        "desktop_pet_ready_for_review:",
        "desktop_pet_running:",
        "desktop_pet_ready:",
        "desktop_pet_action_required:",
        "desktop_pet_reply:",
        "desktop_pet_sending:",
        "desktop_pet_failed_to_send:",
        "desktop_pet_dismiss_update:",
        "desktop_pet_latest:",
        "desktop_pet_more_sessions_below:",
        "desktop_pet_switch_skin:",
        "desktop_pet_restart:",
        "desktop_pet_close:",
        "settings_desc_desktop_pet:",
        "settings_open_desktop_pet:",
        "settings_desktop_pet_started:",
        "settings_desktop_pet_already_running:",
        "settings_desktop_pet_start_failed:",
        "settings_desktop_pet_setup_title:",
        "settings_desktop_pet_setup_prepare:",
        "settings_desktop_pet_setup_load:",
        "settings_desktop_pet_setup_ready:",
        "settings_desktop_pet_setup_starting:",
        "settings_desktop_pet_step_install:",
        "settings_desktop_pet_step_load:",
        "settings_desktop_pet_step_ready:",
        "settings_desktop_pet_launch_ready:",
        "desktop_pet_install_title:",
        "desktop_pet_install_check_webui:",
        "desktop_pet_install_load_skins:",
        "desktop_pet_install_ready:",
        "desktop_pet_ready_toast:",
    ]
    for key in keys:
        assert i18n.count(key) == 11
    assert "desktop_pet_install_title: '正在启动桌面宠物'" in i18n
    assert "desktop_pet_install_title: '正在啟動桌面寵物'" in i18n
    assert "desktop_pet_switch_skin: '切换皮肤'" in i18n
    assert "desktop_pet_switch_skin: '切換皮膚'" in i18n
