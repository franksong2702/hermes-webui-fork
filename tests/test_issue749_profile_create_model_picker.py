"""Regression coverage for #749 profile creation model/provider selection."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import api.profiles as profiles


REPO = Path(__file__).resolve().parent.parent
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")


def _js_function_body(source: str, signature: str) -> str:
    """Return a top-level JavaScript function body using brace balancing."""
    start = source.find(signature)
    assert start != -1, f"missing JS function: {signature}"
    brace_start = source.find("{", start)
    assert brace_start != -1, f"missing opening brace for {signature}"
    depth = 0
    for idx in range(brace_start, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"missing closing brace for {signature}")


def test_profile_create_form_exposes_model_picker():
    assert 'id="profileFormModel"' in PANELS_JS
    assert "_populateProfileFormModelSelect" in PANELS_JS
    assert "profile_model_label" in PANELS_JS
    assert "profile_model_hint" in PANELS_JS


def test_profile_create_payload_preserves_provider_context():
    fn_body = _js_function_body(PANELS_JS, "async function saveProfileForm()")
    assert "profileFormModel" in fn_body
    assert "_modelStateForSelect(modelEl, selectedModel)" in fn_body
    assert "payload.default_model" in fn_body
    assert "payload.model_provider" in fn_body


def test_profile_create_route_passes_model_fields_to_profile_api():
    route_start = ROUTES_PY.find('if parsed.path == "/api/profile/create":')
    assert route_start != -1
    route_body = ROUTES_PY[route_start : ROUTES_PY.find('if parsed.path == "/api/profile/delete":', route_start)]
    assert 'default_model = body.get("default_model"' in route_body
    assert 'model_provider = body.get("model_provider"' in route_body
    assert "default_model=default_model" in route_body
    assert "model_provider=model_provider" in route_body


def test_profile_model_config_writer_persists_default_and_provider(tmp_path):
    profile_dir = tmp_path / "profiles" / "research"
    profile_dir.mkdir(parents=True)

    profiles._write_model_defaults_to_config(
        profile_dir,
        default_model="anthropic/claude-opus-4.6",
        model_provider="nous",
    )

    saved = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"]["default"] == "anthropic/claude-opus-4.6"
    assert saved["model"]["provider"] == "nous"


def test_profile_model_config_writer_preserves_existing_model_settings(tmp_path):
    profile_dir = tmp_path / "profiles" / "research"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "model:\n  base_url: https://gateway.example/v1\n",
        encoding="utf-8",
    )

    profiles._write_model_defaults_to_config(
        profile_dir,
        default_model="gpt-5.5",
        model_provider="openai-codex",
    )

    saved = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"]["base_url"] == "https://gateway.example/v1"
    assert saved["model"]["default"] == "gpt-5.5"
    assert saved["model"]["provider"] == "openai-codex"


def test_profile_model_selection_accepts_catalog_model_with_provider():
    catalog = {
        "groups": [
            {
                "provider": "OpenAI Codex",
                "provider_id": "openai-codex",
                "models": [{"id": "gpt-5.5", "label": "GPT-5.5"}],
            }
        ]
    }

    profiles._validate_profile_model_selection(
        "gpt-5.5",
        "openai-codex",
        available_models=catalog,
    )


def test_profile_model_selection_accepts_provider_qualified_picker_value():
    catalog = {
        "groups": [
            {
                "provider": "Research Gateway",
                "provider_id": "custom:research-gateway",
                "models": [
                    {
                        "id": "@custom:research-gateway:claude-opus-4.6",
                        "label": "claude-opus-4.6",
                    }
                ],
            }
        ]
    }

    default_model, model_provider = profiles._split_webui_provider_model_value(
        "@custom:research-gateway:claude-opus-4.6",
        "custom:research-gateway",
    )

    profiles._validate_profile_model_selection(
        default_model,
        model_provider,
        available_models=catalog,
    )


def test_profile_model_selection_rejects_unknown_model_provider_pair():
    catalog = {
        "groups": [
            {
                "provider": "OpenAI Codex",
                "provider_id": "openai-codex",
                "models": [{"id": "gpt-5.5", "label": "GPT-5.5"}],
            }
        ]
    }

    with pytest.raises(ValueError, match="not available for provider"):
        profiles._validate_profile_model_selection(
            "missing-model",
            "openai-codex",
            available_models=catalog,
        )


def test_profile_create_rejects_unknown_model_before_creating_profile(monkeypatch):
    calls = []

    monkeypatch.setattr(
        profiles,
        "_get_available_models_for_profile_validation",
        lambda: {
            "groups": [
                {
                    "provider": "OpenAI Codex",
                    "provider_id": "openai-codex",
                    "models": [{"id": "gpt-5.5", "label": "GPT-5.5"}],
                }
            ]
        },
    )
    monkeypatch.setattr(
        profiles,
        "_create_profile_fallback",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(ValueError, match="Selected model 'missing-model'"):
        profiles.create_profile_api(
            "research",
            default_model="missing-model",
            model_provider="openai-codex",
        )

    assert calls == []


def test_profile_detail_exposes_model_edit_flow():
    assert 'id="btnEditProfileDetail"' in (REPO / "static" / "index.html").read_text(encoding="utf-8")
    assert "function openProfileEdit" in PANELS_JS
    assert "_renderProfileForm({ mode: 'edit'" in PANELS_JS
    assert "profile_model_hint_edit" in PANELS_JS


def test_profile_edit_payload_updates_existing_profile_instead_of_create():
    fn_body = _js_function_body(PANELS_JS, "async function saveProfileForm()")
    assert "_profileMode === 'edit'" in fn_body
    assert "/api/profile/update" in fn_body
    assert "name: currentName" in fn_body
    assert "clear_model" in fn_body


def test_profile_update_route_passes_model_fields_to_profile_api():
    route_start = ROUTES_PY.find('if parsed.path == "/api/profile/update":')
    assert route_start != -1
    route_body = ROUTES_PY[route_start : ROUTES_PY.find('if parsed.path == "/api/profile/delete":', route_start)]
    assert 'default_model = body.get("default_model"' in route_body
    assert 'model_provider = body.get("model_provider"' in route_body
    assert "update_profile_api" in route_body
    assert "clear_model=clear_model" in route_body


def test_profile_model_config_updater_changes_and_clears_existing_model_settings(tmp_path):
    profile_dir = tmp_path / "profiles" / "research"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "model:\n  base_url: https://gateway.example/v1\n  default: old-model\n  provider: old-provider\n",
        encoding="utf-8",
    )

    profiles._update_model_defaults_in_config(
        profile_dir,
        default_model="gpt-5.5",
        model_provider="openai-codex",
    )

    saved = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"]["base_url"] == "https://gateway.example/v1"
    assert saved["model"]["default"] == "gpt-5.5"
    assert saved["model"]["provider"] == "openai-codex"

    profiles._update_model_defaults_in_config(profile_dir, clear_model=True)

    cleared = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert cleared["model"] == {"base_url": "https://gateway.example/v1"}


def test_profile_update_rejects_unknown_model_before_writing(monkeypatch, tmp_path):
    profile_dir = tmp_path / "profiles" / "research"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "model:\n  default: keep-me\n  provider: keep-provider\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "_resolve_named_profile_home", lambda name: profile_dir)
    monkeypatch.setattr(
        profiles,
        "_get_available_models_for_profile_validation",
        lambda: {
            "groups": [
                {
                    "provider": "OpenAI Codex",
                    "provider_id": "openai-codex",
                    "models": [{"id": "gpt-5.5", "label": "GPT-5.5"}],
                }
            ]
        },
    )

    with pytest.raises(ValueError, match="Selected model 'missing-model'"):
        profiles.update_profile_api(
            "research",
            default_model="missing-model",
            model_provider="openai-codex",
        )

    saved = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"]["default"] == "keep-me"
    assert saved["model"]["provider"] == "keep-provider"


def test_profile_edit_picker_prefills_existing_profile_provider_model():
    fn_body = _js_function_body(PANELS_JS, "async function _populateProfileFormModelSelect")
    assert "const profileModel = profile && profile.model ? String(profile.model) : ''" in fn_body
    assert "const profileProvider = profile && profile.provider ? String(profile.provider) : null" in fn_body
    assert "const modelToApply = profile ? profileModel : (data && data.default_model)" in fn_body
    assert "? (profileProvider || (typeof _providerFromModelValue === 'function'" in fn_body
    assert "_applyModelToDropdown(modelToApply, sel, providerToApply)" in fn_body


def test_profile_update_api_persists_changed_model_and_returns_fresh_metadata(monkeypatch, tmp_path):
    profile_dir = tmp_path / "profiles" / "research"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "model:\n  base_url: https://gateway.example/v1\n  default: old-model\n  provider: old-provider\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: False)
    monkeypatch.setattr(profiles, "_resolve_named_profile_home", lambda name: profile_dir)
    monkeypatch.setattr(
        profiles,
        "_get_available_models_for_profile_validation",
        lambda: {
            "groups": [
                {
                    "provider": "OpenAI Codex",
                    "provider_id": "openai-codex",
                    "models": [{"id": "gpt-5.5", "label": "GPT-5.5"}],
                }
            ]
        },
    )
    monkeypatch.setattr(
        profiles,
        "list_profiles_api",
        lambda: [
            {
                "name": "research",
                "path": str(profile_dir),
                "is_default": False,
                "is_active": False,
                "model": "old-model",
                "provider": "old-provider",
            }
        ],
    )

    result = profiles.update_profile_api(
        "research",
        default_model="gpt-5.5",
        model_provider="openai-codex",
    )

    saved = yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))
    assert saved["model"] == {
        "base_url": "https://gateway.example/v1",
        "default": "gpt-5.5",
        "provider": "openai-codex",
    }
    assert result["model"] == "gpt-5.5"
    assert result["provider"] == "openai-codex"


def test_profile_update_route_calls_update_api_and_invalidates_model_cache(monkeypatch):
    import api.config as config
    import api.routes as routes

    calls = []
    invalidated = []
    captured = {}

    def fake_update_profile_api(name, *, default_model=None, model_provider=None, clear_model=False):
        calls.append(
            {
                "name": name,
                "default_model": default_model,
                "model_provider": model_provider,
                "clear_model": clear_model,
            }
        )
        return {"name": name, "model": default_model, "provider": model_provider}

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda handler: {
            "name": "research",
            "default_model": "gpt-5.5",
            "model_provider": "openai-codex",
        },
    )
    monkeypatch.setattr(profiles, "update_profile_api", fake_update_profile_api)
    monkeypatch.setattr(config, "invalidate_models_cache", lambda: invalidated.append(True))
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            {"payload": payload, "status": status}
        ) or True,
    )

    assert routes.handle_post(object(), SimpleNamespace(path="/api/profile/update")) is True

    assert calls == [
        {
            "name": "research",
            "default_model": "gpt-5.5",
            "model_provider": "openai-codex",
            "clear_model": False,
        }
    ]
    assert invalidated == [True]
    assert captured == {
        "payload": {
            "ok": True,
            "profile": {"name": "research", "model": "gpt-5.5", "provider": "openai-codex"},
        },
        "status": 200,
    }
