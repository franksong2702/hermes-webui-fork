use serde::Deserialize;
use std::env;
use tauri::menu::{MenuBuilder, SubmenuBuilder};
use tauri::{Emitter, Listener, Manager, Url};

const CLOSE_PET_MENU_ID: &str = "close_pet";
const RESTART_PET_MENU_ID: &str = "restart_pet";
const PET_CONTEXT_MENU_EVENT: &str = "pet-context-menu";
const PET_SKIN_CHANGE_EVENT: &str = "pet-skin-change";
const PET_RESTART_REQUESTED_EVENT: &str = "pet-restart-requested";
const SKIN_MENU_PREFIX: &str = "skin:";
const HERMES_DESKTOP_PET_BASE_URL_ENV: &str = "HERMES_DESKTOP_PET_BASE_URL";
const FALLBACK_WEBUI_BASE_URL: &str = "http://127.0.0.1:8787";

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct PetContextMenuPayload {
    skins: Vec<PetSkin>,
    active_skin_id: Option<String>,
    menu_labels: Option<PetContextMenuLabels>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct PetContextMenuLabels {
    switch_skin: Option<String>,
    restart_pet: Option<String>,
    close_pet: Option<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct PetSkin {
    id: String,
    display_name: String,
}

fn fallback_skins() -> Vec<PetSkin> {
    vec![
        PetSkin {
            id: "keeper".into(),
            display_name: "May".into(),
        },
        PetSkin {
            id: "shiba".into(),
            display_name: "shiba".into(),
        },
    ]
}

fn pet_context_menu_payload(payload: &str) -> PetContextMenuPayload {
    serde_json::from_str(payload).unwrap_or_else(|_| PetContextMenuPayload {
        skins: fallback_skins(),
        active_skin_id: Some("keeper".into()),
        menu_labels: None,
    })
}

fn menu_label(value: Option<&String>, fallback: &str) -> String {
    let label = value
        .map(|raw| raw.trim())
        .filter(|raw| !raw.is_empty())
        .unwrap_or(fallback);
    let cleaned = label
        .chars()
        .filter(|ch| !ch.is_control())
        .take(64)
        .collect::<String>()
        .trim()
        .to_string();
    if cleaned.is_empty() {
        fallback.into()
    } else {
        cleaned
    }
}

fn valid_skin_id(id: &str) -> bool {
    !id.is_empty()
        && id
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '_' || ch == '-')
}

fn sanitize_skin(skin: PetSkin) -> Option<PetSkin> {
    if !valid_skin_id(&skin.id) {
        return None;
    }
    let display_name = skin
        .display_name
        .trim()
        .chars()
        .filter(|ch| !ch.is_control())
        .take(64)
        .collect::<String>();
    Some(PetSkin {
        id: skin.id,
        display_name: if display_name.is_empty() {
            "skin".into()
        } else {
            display_name
        },
    })
}

fn normalize_loopback_base_url(raw: &str) -> Option<Url> {
    let mut url = Url::parse(raw.trim()).ok()?;
    if !matches!(url.scheme(), "http" | "https") {
        return None;
    }
    let host = url.host_str()?.to_ascii_lowercase();
    if host != "localhost" && host != "::1" && !host.starts_with("127.") {
        return None;
    }
    if host == "::1" {
        url.set_host(Some("localhost")).ok()?;
    }
    url.set_path("/");
    url.set_query(None);
    url.set_fragment(None);
    Some(url)
}

fn webui_base_url() -> Url {
    env::var(HERMES_DESKTOP_PET_BASE_URL_ENV)
        .ok()
        .and_then(|raw| normalize_loopback_base_url(&raw))
        .unwrap_or_else(|| {
            Url::parse(FALLBACK_WEBUI_BASE_URL).expect("fallback WebUI URL is valid")
        })
}

fn pet_page_url(base: &Url, path: &str) -> Url {
    let mut url = base.clone();
    url.set_path(path);
    url.set_query(None);
    url.set_fragment(None);
    url
}

fn navigate_pet_windows(app: &tauri::App) {
    let base = webui_base_url();
    if let Some(window) = app.get_webview_window("pet") {
        let _ = window.navigate(pet_page_url(&base, "/pet"));
    }
    if let Some(window) = app.get_webview_window("pet_bubbles") {
        let _ = window.navigate(pet_page_url(&base, "/pet/bubbles"));
    }
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            navigate_pet_windows(app);
            let handle = app.handle().clone();
            app.listen(PET_CONTEXT_MENU_EVENT, move |event| {
                let payload = pet_context_menu_payload(event.payload());
                let handle = handle.clone();
                let menu_handle = handle.clone();
                let _ = handle.run_on_main_thread(move || {
                    let Some(window) = menu_handle.get_webview_window("pet") else {
                        return;
                    };
                    let labels = payload.menu_labels.as_ref();
                    let switch_skin_label = menu_label(
                        labels.and_then(|item| item.switch_skin.as_ref()),
                        "Switch skin",
                    );
                    let restart_pet_label = menu_label(
                        labels.and_then(|item| item.restart_pet.as_ref()),
                        "Restart pet",
                    );
                    let close_pet_label =
                        menu_label(labels.and_then(|item| item.close_pet.as_ref()), "Close pet");
                    let mut skin_builder = SubmenuBuilder::new(&menu_handle, switch_skin_label);
                    let active_skin_id = payload
                        .active_skin_id
                        .as_deref()
                        .filter(|id| valid_skin_id(id))
                        .unwrap_or("keeper");
                    let mut skins: Vec<PetSkin> = payload
                        .skins
                        .into_iter()
                        .filter_map(sanitize_skin)
                        .collect();
                    if skins.is_empty() {
                        skins = fallback_skins();
                    }
                    for skin in skins {
                        let mut label = skin.display_name;
                        if skin.id == active_skin_id {
                            label = format!("{} ✓", label);
                        }
                        skin_builder =
                            skin_builder.text(format!("{SKIN_MENU_PREFIX}{}", skin.id), label);
                    }
                    let Ok(skin_menu) = skin_builder.build() else {
                        return;
                    };
                    let Ok(menu) = MenuBuilder::new(&menu_handle)
                        .item(&skin_menu)
                        .separator()
                        .text(RESTART_PET_MENU_ID, restart_pet_label)
                        .text(CLOSE_PET_MENU_ID, close_pet_label)
                        .build()
                    else {
                        return;
                    };
                    let _ = window.popup_menu(&menu);
                });
            });
            Ok(())
        })
        .on_menu_event(|app, event| {
            let id = event.id().as_ref();
            if let Some(skin_id) = id.strip_prefix(SKIN_MENU_PREFIX) {
                if !valid_skin_id(skin_id) {
                    return;
                }
                let skin_id = skin_id.to_string();
                let _ = app.emit_to("pet", PET_SKIN_CHANGE_EVENT, skin_id.clone());
                let _ = app.emit_to("pet_bubbles", PET_SKIN_CHANGE_EVENT, skin_id);
                return;
            }
            match id {
                CLOSE_PET_MENU_ID => app.exit(0),
                RESTART_PET_MENU_ID => {
                    let _ = app.emit_to("pet", PET_RESTART_REQUESTED_EVENT, ());
                }
                _ => {}
            }
        })
        .run(tauri::generate_context!())
        .expect("failed to run Hermes desktop pet");
}
