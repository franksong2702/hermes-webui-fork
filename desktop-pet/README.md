# Hermes Desktop Pet

This is a thin desktop shell for the optional standalone Hermes pet page.

It intentionally does not reimplement Hermes UI. The WebUI launch endpoint
passes the current loopback WebUI base URL to the native shell, and the shell
opens:

```text
<active WebUI URL>/pet
```

For example, a WebUI running on `http://127.0.0.1:8788` starts the pet against
`http://127.0.0.1:8788/pet`. If the shell is started directly without WebUI's
launcher environment, it falls back to `http://127.0.0.1:8787`.

The Hermes WebUI server must already be running first. Starting WebUI alone does
not show the pet; the pet only appears when this Tauri shell is launched. The
WebUI Settings -> Appearance desktop pet switch first checks `/api/pet/status`:
if a shell is already installed or built, it launches it immediately; otherwise
the Appearance row shows inline setup progress, prepares the local shell through
`/api/pet/install`, and starts the pet when the shell is ready. Turning the same
switch off calls `/api/pet/close`.

For local testing:

Start WebUI on any loopback port, for example `HERMES_WEBUI_PORT=8788 ./start.sh`.

Then run the shell from this directory:

```bash
npm install
npm run dev
```

Window intent:

- transparent background
- no native decorations
- always on top
- skipped from the taskbar / dock where supported
- pet-sized transparent viewport
- right-click menu for switching detected skins, restarting the pet, or closing it

The default bundled skin is `keeper` (`May`). Additional skins can be added under
`static/pets/<id>/pet.json` plus a local spritesheet; the WebUI exposes the
detected list through `/api/pet/skins`.

The shell is backed by lazy WebUI endpoints:

- `/pet` serves the standalone pet page.
- `/api/pet/attention` returns sessions that need attention.
- `/api/pet/skins` lists bundled and locally added skins.
- `/api/pet/navigation` lets an already-open WebUI tab consume pet commands.
- `/api/pet/open_session` queues a session jump or reply through the existing
  WebUI composer path.
- `/api/pet/status` checks whether a launchable native shell is already present.
- `/api/pet/install` prepares the local native shell when it is missing.
- `/api/pet/launch` starts the native desktop shell from a loopback WebUI
  request when an installed app, built binary, or local Tauri dev setup is
  available. The launch environment includes `HERMES_DESKTOP_PET_BASE_URL`
  (and `HERMES_WEBUI_BASE_URL`) so the shell follows the active WebUI URL/port.
  Launch is single-instance: an already-running pet is reused instead of
  starting another process.
- `/api/pet/close` stops the running desktop pet from a loopback WebUI request.

This is a desktop-only beta for macOS and Windows. macOS has been locally
verified; Windows is source-compatible but should be treated as beta until
verified on a Windows host. It is not part of the mobile WebUI surface, and
packaging/signing/release artifacts are intentionally outside this first
integration slice.
