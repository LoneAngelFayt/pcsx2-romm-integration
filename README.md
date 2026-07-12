# pcsx2-romm-integration

A [LinuxServer Docker Mod](https://docs.linuxserver.io/general/container-customization) for [PCSX2](https://pcsx2.net/) that connects to [RomM](https://github.com/rommapp/romm). Pick a game from the RomM web UI and the mod launches it in PCSX2, streaming the session back via Selkies.

---

## What It Does

This mod installs a small HTTP broker inside the [linuxserver/pcsx2](https://docs.linuxserver.io/images/docker-pcsx2/) container that:

1. Exposes an API on port 8000 so RomM can request game launches
2. Manages the PCSX2 process lifecycle (start, stop, game switching, dashboard mode)
3. Saves game state by sending an F-key to the PCSX2 window (via xdotool) before exit
4. Patches Selkies and PCSX2 at init time for reliable controller and socket handling
5. Supervises itself via s6-overlay (auto-restarts on crash)

---

## Requirements

- **Base image:** [linuxserver/pcsx2](https://docs.linuxserver.io/images/docker-pcsx2/) (Wayland/Selkies already included)
- **RomM instance** with streaming configured (see [RomM Configuration](#romm-configuration))
- **Shared ROM volume** mounted at the same path in both containers
- **Network access** from RomM's backend to the broker at `pcsx2:8000`

---

## Installation

Add `DOCKER_MODS` to your PCSX2 container in `docker-compose.yml`:

```yaml
services:
  pcsx2:
    image: lscr.io/linuxserver/pcsx2:latest
    container_name: pcsx2
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=America/Chicago
      - DOCKER_MODS=ghcr.io/loneangelfayt/pcsx2-romm-integration-mod:latest
      - BROKER_SECRET=your_secret_here
      - ROM_ROOT=/romm/library
    ports:
      - 8000:8000   # broker API
    volumes:
      - ./config:/config
      - /mnt/roms:/romm/library   # must match ROM_ROOT; shared with RomM
```

Then recreate the container:

```bash
docker compose up -d --force-recreate pcsx2
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BROKER_SECRET` | *(none)* | Shared secret for authentication. Sent as the `X-Broker-Secret` header. If unset, all requests are accepted — not recommended on a shared network. |
| `BROKER_PORT` | `8000` | Port the broker HTTP server listens on. |
| `ROM_ROOT` | `/romm/library` | Root path inside the container where ROMs are mounted. Requests with a `rom_path` outside this directory are rejected. |
| `PINE_WAIT` | `20.0` | Maximum seconds to poll for save state write completion after sending the save keypress. Polling stops early once the write is detected. Increase for slow disks or large games. (Name kept for backwards compatibility — the broker now confirms writes for the xdotool save path, not PINE.) |
| `SAVE_SLOT` | `10` | Default save state slot (1–10) for `/save-and-exit` when no `slot` is specified. Slot 10 is recommended as an auto-save slot, leaving 1–9 free for manual use. |
| `SSTATE_DIR` | `/config/.config/PCSX2/sstates` | Where PCSX2 writes save state files. Served and written by the `/state-file` endpoints for RomM's save-state sync. |
| `STATE_GET_WAIT` | `30.0` | Max seconds `GET /state-file` blocks waiting for an in-flight save to finish before serving the slot file. |
| `RESUME_LOAD_WAIT` | `90.0` | Max seconds a `load_slot` launch waits for the new game VM to reach running state before giving up on the deferred state load. |
| `RESUME_LOAD_SETTLE` | `3.0` | Seconds to let the game settle after the VM reports running, before the deferred `load_slot` state load fires. |

---

## RomM Configuration

Add a `streaming` block to RomM's `config.yml`:

```yaml
streaming:
  enabled: true
  containers:
    - platform: ps2
      host: "https://192.168.x.x:3001"        # browser-facing Selkies web UI
      broker_host: "http://pcsx2:8000"        # server-to-server broker API (optional — derived from host if omitted)
      label: "PCSX2"
```

The `platform` value must match the platform slug used for your PS2 ROMs in RomM. The ROM volume must be mounted at the same path in both containers. If RomM sees a ROM at `/romm/library/ps2/game.chd`, the PCSX2 container must also have that file at `/romm/library/ps2/game.chd`.

---

## API Reference

All endpoints return JSON. If `BROKER_SECRET` is configured, include `X-Broker-Secret: <secret>` in every request.

---

### `GET /health`

Returns `200 OK` if the broker is running.

```json
{ "status": "ok" }
```

---

### `GET /status`

Returns the current session.

```json
{
  "active": true,
  "rom_path": "/romm/library/ps2/game.chd",
  "rom_name": "game",
  "started_at": "2026-01-01T00:00:00Z"
}
```

Returns `{"active": false, ...}` when no game is running.

---

### `POST /launch`

Kills any running game and launches a new ROM. Returns immediately; launch runs in a background thread.

```json
{ "rom_path": "/romm/library/ps2/game.chd", "rom_name": "Game Title", "load_slot": 3 }
```

- `rom_path` must exist and be under `ROM_ROOT`
- `load_slot` (optional, 1–10) — resume-from-state: once the game VM reports running (checked via PINE `EMU_STATUS`, up to `RESUME_LOAD_WAIT`, plus a `RESUME_LOAD_SETTLE` grace), the broker loads that state slot. Push the state file via `PUT /state-file` before launching.
- Returns `409` if a save is in progress
- Returns `400` if `rom_path` is missing or outside `ROM_ROOT`, or `load_slot` is not an integer 1–10
- Returns `422` if `rom_path` does not exist

```json
{ "status": "launching", "rom_path": "/romm/library/ps2/game.chd" }
```

---

### `DELETE /launch`

Stops the current game and returns PCSX2 to dashboard mode. Runs in background.

```json
{ "status": "resetting" }
```

---

### `POST /save-and-exit`

Saves the current game state via xdotool F-key, kills PCSX2, then relaunches the dashboard.

Save states are written to `SSTATE_DIR` as `{SERIAL} ({CRC}).{slot:02d}.p2s`.

**Request body:**
```json
{ "slot": 10, "wait": true }
```

| Field | Default | Description |
|---|---|---|
| `slot` | `SAVE_SLOT` env var | Save state slot (1–10). `0` is accepted as a legacy value and remapped to `SAVE_SLOT`. |
| `wait` | `true` | `true` = blocking (responds after save+kill complete); `false` = fire-and-forget (responds immediately, save+kill in background) |

**`wait=true` response:**
```json
{ "status": "ok", "saved": true, "slot": 10 }
```

**`wait=false` response:**
```json
{ "status": "queued", "slot": 10 }
```

- Returns `409` if no game is running or if a save is already in progress
- Returns `400` if `slot` is not an integer 0–10

---

### `POST /save-state`

Saves the current game to a slot without exiting. The keypress goes to the PCSX2 window via xdotool and the save runs in the background, so a `200` means the keystroke was sent — not that the write finished. Watch `/status` (`save_in_progress`) to confirm.

```json
{ "slot": 1 }
```

| Field | Default | Description |
|---|---|---|
| `slot` | `1` | Save state slot (1–10) |

```json
{ "status": "saving", "slot": 1 }
```

- Returns `409` if no game is running or a save is already in progress
- Returns `400` if `slot` is not an integer 1–10

---

### `POST /load-state`

Loads a save state into the running game. This one blocks until the keystroke is dispatched.

```json
{ "slot": 1 }
```

| Field | Default | Description |
|---|---|---|
| `slot` | `1` | Save state slot (1–10) |

```json
{ "status": "ok", "loaded": true, "slot": 1 }
```

- Returns `409` if no game is running
- Returns `400` if `slot` is not an integer 1–10
- Returns `503` if the PCSX2 window can't be reached (the broker itself is fine)

---

### `GET /state-file?slot=N`

Serves the newest `.p2s` state file for the slot as raw bytes, for RomM's centralized save-state sync. If a save is in flight the response blocks until the write completes (up to `STATE_GET_WAIT`, default 30 s), so a GET fired right after `/save-state` always carries the finished file.

| Query param | Default | Description |
|---|---|---|
| `slot` | `0` | Save state slot (1–10); `0` resolves to `SAVE_SLOT` |

- Response body is `application/octet-stream`; the emulator's own filename is echoed in the `X-State-Filename` header
- Returns `404` if no state file exists for the slot
- Returns `400` if `slot` is not an integer 0–10

---

### `PUT /state-file?filename=NAME`

Writes a state file into the sstates directory, used by RomM to hydrate a freshly claimed container with the user's stored states. `NAME` is the filename a previous GET returned — written back verbatim so PCSX2 recognises the slot.

- Body is the raw file content (`Content-Length` required, max 256 MB)
- `NAME` must be a bare `.p2s` basename; path components are rejected
- The write is atomic (temp file + rename) and the file is chowned to `abc` so PCSX2 can overwrite the slot later

```json
{ "status": "ok", "filename": "SLUS-12345 (ABCD1234).03.p2s" }
```

---

### `POST /volume`

Sets the audio output volume.

```json
{ "level": 75 }
```

- `level` must be an integer 0–100
- Returns `400` if `level` is out of range
- Returns `500` if `pactl` fails (PulseAudio not ready)

```json
{ "status": "ok", "level": 75 }
```

---

### `POST /mute`

Sets or toggles the mute state.

```json
{ "mute": true }
```

| Field | Default | Description |
|---|---|---|
| `mute` | *(omit to toggle)* | `true` = mute, `false` = unmute, omit = toggle |

```json
{ "status": "ok", "mute": true }
```

---

### `POST /cleanup`

Restarts the Selkies process to flush stale gamepad socket connections. Selkies is back within a few seconds via s6 supervision.

```json
{ "status": "cleanup started" }
```

Use this if controller inputs become unresponsive. Under normal operation the `reader.at_eof()` patch applied at container init prevents connection buildup.

---

## Verifying It's Running

```bash
docker logs pcsx2 | grep broker
```

Expected startup:
```
14:20:15 [broker] INFO Broker starting — waiting 5s for desktop...
14:20:21 [broker] INFO ROM broker listening on port 8000
14:20:21 [broker] INFO Shared secret auth enabled
14:20:23 [broker] INFO Launching PCSX2 (rom=dashboard)
14:20:24 [broker] INFO PCSX2 launched (PID 42)
```

Expected on game launch:
```
14:22:10 [broker] INFO Stopping PCSX2 (PID 42)...
14:22:10 [broker] INFO Launching PCSX2 (rom=/romm/library/ps2/game.chd)
14:22:11 [broker] INFO PCSX2 launched (PID 123)
```

Expected on save-and-exit:
```
14:25:45 [broker] INFO xdotool: F1 sent to window 0x3a00007 (slot 10) — waiting for write (max 20.0s)
14:25:48 [broker] INFO Stopping PCSX2 (PID 123)...
14:25:49 [broker] INFO Launching PCSX2 (rom=dashboard)
14:25:49 [broker] INFO PCSX2 launched (PID 456)
```

---

## Pinning to a Version

```yaml
# Exact version — never changes
- DOCKER_MODS=ghcr.io/loneangelfayt/pcsx2-romm-integration-mod:v1.2.0

# Minor pin — gets patches only
- DOCKER_MODS=ghcr.io/loneangelfayt/pcsx2-romm-integration-mod:v1.2

# Always latest release
- DOCKER_MODS=ghcr.io/loneangelfayt/pcsx2-romm-integration-mod:latest
```

Available versions: [Packages page](https://github.com/LoneAngelFayt/pcsx2-romm-integration/pkgs/container/pcsx2-romm-integration-mod)

---

## Roadmap

| Feature | Status | Notes |
|---|---|---|
| Game launching via RomM | ✅ Done | `POST /launch` |
| Save state on exit | ✅ Done | `POST /save-and-exit` — xdotool F-key to PCSX2 window, slot 10 default |
| Return to dashboard on exit | ✅ Done | Automatic after any exit path |
| Volume control | ✅ Done | `POST /volume` and `POST /mute` via `pactl` |
| Manual save state (no exit) | ✅ Done | `POST /save-state` with slot selection |
| Manual load state | ✅ Done | `POST /load-state` with slot selection |
| RomM save state export/import | ✅ Done | `GET`/`PUT /state-file` — RomM pulls saves into the library and hydrates slots on claim |

---

## Troubleshooting

**Mod doesn't apply or broker doesn't start**
- Verify the image name: `ghcr.io/loneangelfayt/pcsx2-romm-integration-mod`
- Run `docker compose up` (no `-d`) to see full startup output
- Check that the base image is `lscr.io/linuxserver/pcsx2:latest` or compatible

**Black screen for more than 60 seconds after launch**
- A 15–30 second black screen is normal (PS2 BIOS boot sequence)
- If it persists, confirm BIOS files are in place: `docker exec pcsx2 ls /config/bios`
- Check PCSX2 emulog: `docker exec pcsx2 tail -50 /config/.config/PCSX2/logs/emulog.txt`

**Controllers not working**
- Try `POST /cleanup` to restart Selkies and flush stale gamepad sockets
- Confirm `LD_PRELOAD` is set correctly in the broker environment (check broker logs)
- Run: `docker exec pcsx2 ls /tmp/selkies_js*.sock` to verify Selkies socket files exist

**Save state fails**
- Check broker logs for xdotool output: `docker logs pcsx2 | grep xdotool`
- Verify the PCSX2 window is found: look for `xdotool: found window` in logs
- If window not found, confirm `xdotool` is installed: `docker exec pcsx2 which xdotool`
- Increase `PINE_WAIT` (default 20s) if saves are slow: `PINE_WAIT=30.0`
- Save files land at `SSTATE_DIR` (`/config/.config/PCSX2/sstates` by default): `docker exec pcsx2 ls /config/.config/PCSX2/sstates`

**PCSX2 crashes immediately after game launch**
- Check emulog for GPU or BIOS errors
- Ensure the game file is not corrupted: `.chd`, `.iso`, `.bin/.cue` are all supported

**RomM doesn't show the PCSX2 play button**
- Confirm `streaming.enabled: true` in RomM's `config.yml`
- Confirm the `platform` slug matches the ROM platform in RomM
- Restart RomM after config changes: `docker compose restart romm`
- Check the streaming config API: `curl http://romm:5000/api/streaming/config`

---

## Versions

This project follows [Semantic Versioning](https://semver.org/):

| Change type | Example | Bump |
|---|---|---|
| Bug fix | `fix: PINE socket discovery` | `1.0.0 → 1.0.1` |
| New feature | `feat: save-and-exit endpoint` | `1.0.1 → 1.1.0` |
| Breaking change | `feat!: new broker protocol` | `1.1.0 → 2.0.0` |

Releases are created automatically on merge to `main`. See [Releases](https://github.com/LoneAngelFayt/pcsx2-romm-integration/releases) for the full changelog.

---

## License

[GPLv3](LICENSE)
