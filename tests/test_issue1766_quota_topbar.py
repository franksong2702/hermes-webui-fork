from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INDEX = (REPO / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
COMMANDS_JS = (REPO / "static" / "commands.js").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_quota_indicator_is_near_model_picker_in_composer_chrome():
    model_idx = INDEX.find('id="composerModelChip"')
    quota_idx = INDEX.find('id="providerQuotaChip"')

    assert model_idx != -1, "composer model chip must exist"
    assert quota_idx != -1, "provider quota chip must exist"
    assert model_idx < quota_idx < INDEX.find('id="composerReasoningWrap"'), (
        "quota chip should sit next to the model picker, before reasoning/toolset chrome"
    )
    assert 'class="provider-quota-chip"' in INDEX
    assert 'hidden' in INDEX[quota_idx - 200 : quota_idx + 400]


def test_quota_indicator_fetches_provider_quota_on_boot():
    assert "function refreshProviderQuotaIndicator" in UI_JS
    assert "function _providerQuotaEndpoint" in UI_JS
    assert '"/api/provider/quota"' in UI_JS or "'/api/provider/quota'" in UI_JS
    assert "refreshProviderQuotaIndicator" in BOOT_JS


def test_quota_indicator_hides_unsupported_or_failed_statuses():
    render_idx = UI_JS.find("function renderProviderQuotaIndicator")
    assert render_idx != -1, "renderProviderQuotaIndicator helper must exist"
    render_block = UI_JS[render_idx : UI_JS.find("function ", render_idx + 1)]

    assert "providerQuotaChip" in render_block
    assert "_resetProviderQuotaChip" in render_block
    assert "chip.hidden=true" in UI_JS
    assert "status.status!=='available'" in render_block
    assert "!status.quota" in render_block
    assert "unsupported" not in render_block.lower(), "ambient chip should disappear instead of showing noisy unsupported text"


def test_quota_indicator_formats_openrouter_and_account_limit_shapes():
    assert "function _providerQuotaIndicatorText" in UI_JS
    assert "limit_remaining" in UI_JS
    assert "account_limits" in UI_JS
    assert "remaining_percent" in UI_JS
    assert "provider-quota-chip" in CSS


def test_quota_indicator_uses_selected_provider_and_refreshes_on_changes():
    assert "function _currentProviderQuotaProviderId" in UI_JS
    assert "encodeURIComponent(providerId)" in UI_JS
    assert "refresh=1" in UI_JS
    assert "_providerQuotaRequestSeq" in UI_JS, "stale quota requests must not overwrite a newer provider selection"

    boot_model_change_idx = BOOT_JS.find("$('modelSelect').onchange")
    assert boot_model_change_idx != -1, "model picker change handler must exist"
    boot_model_change_end = BOOT_JS.find("if(typeof _checkProviderMismatch", boot_model_change_idx)
    boot_model_change_block = BOOT_JS[boot_model_change_idx:boot_model_change_end]
    assert "refreshProviderQuotaIndicator" in boot_model_change_block
    assert "refreshProviderQuotaIndicator({refresh:true})" in COMMANDS_JS


def test_quota_indicator_refreshes_after_session_model_resolution_and_usage_changes():
    resolve_idx = SESSIONS_JS.find("function _resolveSessionModelForDisplaySoon")
    assert resolve_idx != -1, "session model resolution path must exist"
    resolve_block = SESSIONS_JS[resolve_idx : SESSIONS_JS.find("function ", resolve_idx + 1)]
    assert "refreshProviderQuotaIndicator" in resolve_block

    assert MESSAGES_JS.count("refreshProviderQuotaIndicator({refresh:true})") >= 2, (
        "completed turns should force-refresh quota so post-usage limits are reflected"
    )
    assert "startData.effective_model_provider" in MESSAGES_JS


def test_quota_indicator_handles_loading_and_near_limit_states_without_noise():
    render_idx = UI_JS.find("function renderProviderQuotaIndicator")
    assert render_idx != -1, "renderProviderQuotaIndicator helper must exist"
    render_block = UI_JS[render_idx : UI_JS.find("async function refreshProviderQuotaIndicator", render_idx)]

    assert "status.status==='loading'" in render_block
    assert "aria-busy" in render_block
    assert "provider-quota-chip-low" in render_block
    assert "provider-quota-chip-empty" in render_block
    assert "_providerQuotaRemainingRatio" in UI_JS
