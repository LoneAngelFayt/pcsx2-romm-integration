# pcsx2-romm-integration

A [LinuxServer Docker Mod](https://docs.linuxserver.io/general/container-customization) for [PCSX2](https://pcsx2.net/) that connects to [RomM](https://github.com/rommapp/romm). Pick a game from the RomM web UI and the mod launches it in PCSX2, streaming the session back via Selkies.

---

## What It Does

This mod installs a small HTTP broker inside the [linuxserver/pcsx2](https://docs.linuxserver.io/images/docker-pcsx2/) container that:

1. Exposes an API on port 8000 so RomM can request game launches
2. Manages the PCSX2 process lifecycle (start, stop, game switching, dashboard mode)
3. Saves/loads game state via PCSX2's native PINE IPC socket, falling back to xdotool F-key delivery if PINE is unreachable
4. Patches Selkies at container init, and PCSX2.ini before every launch, for reliable controller and PINE/save-state handling
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

## Memory Card Setup (required for save sync)

**The PCSX2 memory card must be set to `Folder` type, not `File`.** This is the
standard for this mod, and in-game save sync depends on it.

A `File` card is a single monolithic 8 MB image holding every game's saves at
once, so it cannot be synced per game or isolated per user. A `Folder` card
stores each game in its own directory keyed by the game's serial ID (for
example `BASLUS-20267DOTHACK/`), which is what lets the broker sync and hydrate
one game's save without touching any other.

Set it in PCSX2 under **Settings -> Memory Cards**: create a new card of type
`Folder` and assign it to **Slot 1** (Slot 1 is the only slot synced). Confirm
`inis/PCSX2.ini` shows the folder card on `Slot1_Filename`:

```ini
[MemoryCards]
Slot1_Enable = true
Slot1_Filename = my-folder-card.ps2   # this is a directory on disk, not a file
```

> Save **states** (`.p2s` snapshots, synced via `/state-file`) do not depend on
> the memory card type. This requirement is only for in-game memory-card saves.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BROKER_SECRET` | *(none)* | Shared secret for authentication. Sent as the `X-Broker-Secret` header. If unset, all requests are accepted, which is not safe on a shared network. |
| `BROKER_PORT` | `8000` | Port the broker HTTP server listens on. |
| `ROM_ROOT` | `/romm/library` | Root path inside the container where ROMs are mounted. Requests with a `rom_path` outside this directory are rejected. |
| `PINE_WAIT` | `20.0` | Maximum seconds to poll for save state write completion after a save is accepted, whether the save was delivered via PINE (PCSX2's native IPC socket, tried first) or the xdotool F-key fallback. Polling stops early once the write is detected. Increase for slow disks or large games. |
| `SAVE_SLOT` | `10` | Default save state slot (1–10) for `/save-and-exit` when no `slot` is specified. Slot 10 is recommended as an auto-save slot, leaving 1–9 free for manual use. |
| `SSTATE_DIR` | `/config/.config/PCSX2/sstates` | Where PCSX2 writes save state files. Served and written by the `/state-file` endpoints for RomM's save-state sync. |
| `STATE_GET_WAIT` | `30.0` | Max seconds `GET /state-file` blocks waiting for an in-flight save to finish before serving the slot file. |
| `RESUME_LOAD_WAIT` | `90.0` | Max seconds a `load_slot` launch waits for the new game VM to reach running state before giving up on the deferred state load. |
| `RESUME_LOAD_SETTLE` | `3.0` | Seconds to let the game settle after the VM reports running, before the deferred `load_slot` state load fires. |
| `SAVE_DATA_ROOT` | `/config/.config/PCSX2` | Override for the PCSX2 data dir the `/save-file` and `/memory-card` endpoints operate on. Only the `memcards/` subtree under it is synced. |
| `BROKER_INITIAL_SLOT` | `1` | Fallback save state slot the broker assumes PCSX2 starts on when `SaveStateSlot` can't be read from `PCSX2.ini`. Only affects xdotool slot cycling; PINE saves and loads carry the target slot directly. |
| `PCSX2_LOG_PATH` | `/config/pcsx2-qt.log` | File that captures pcsx2-qt's stdout/stderr (renderer, Vulkan, Qt errors). Appended across launches. |
| `BROKER_LOG_LEVEL` | `INFO` | Broker log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). Note: the xdotool window-search success logs (`xdotool: found window ...`) are DEBUG-level; set `BROKER_LOG_LEVEL=DEBUG` to see them. |
| `PUID` / `PGID` | `1000` / `1000` | Standard LinuxServer container UID/GID. The broker also uses them to `chown` the files it writes on PCSX2's behalf (`pcsx2-qt.log`, pulled save-state files, restored save and memory-card files) so pcsx2-qt, which runs as `abc`, can read and overwrite them later. |

---

## RomM Configuration

Add a `streaming` block to RomM's `config.yml`:

```yaml
streaming:
  enabled: true
  containers:
    - platform: ps2
      host: "https://192.168.x.x:3001"        # browser-facing Selkies web UI
      broker_host: "http://pcsx2:8000"        # server-to-server broker API (optional; derived from host if omitted)
      label: "PCSX2"
```

The `platform` value must match the platform slug used for your PS2 ROMs in RomM. The ROM volume must be mounted at the same path in both containers. If RomM sees a ROM at `/romm/library/ps2/game.chd`, the PCSX2 container must also have that file at `/romm/library/ps2/game.chd`.

---

## API Reference

All endpoints return JSON except the raw-bytes/zip bodies noted below. If `BROKER_SECRET` is configured, include `X-Broker-Secret: <secret>` in every request; a missing or wrong secret returns `403 {"error": "forbidden"}`. `GET /health` is the one exception. It never checks the secret, so it works as an unauthenticated container healthcheck.

JSON request bodies over 64 KB are rejected with `413`; a body that isn't valid JSON, or isn't a JSON object, is rejected with `400`. This applies to every `POST` endpoint below that takes a body (`/launch`, `/save-and-exit`, `/volume`, `/mute`, `/save-state`, `/load-state`).

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
  "started_at": "2026-01-01T00:00:00Z",
  "relaunch_abandoned": false
}
```

`active` means a pcsx2-qt process is alive. It does **not** mean a game is running: the idle dashboard is a normal pcsx2-qt process, so an idle container reports `active: true`. To test for a running game, check `rom_path`, which is non-null only when a ROM is loaded and `null` on the dashboard. `rom_name` and `started_at` follow `rom_path`, and all three are reported as `null` whenever `active` is `false`, whatever the broker still holds internally.

`relaunch_abandoned` turns `true` once PCSX2 has exited within 5 seconds three times in a row and the broker has stopped restarting it. Nothing is running, and nothing will recover it without an explicit `POST /launch`. The two fields together tell you which state a quiet container is in:

| `active` | `relaunch_abandoned` | Meaning |
|---|---|---|
| `true` | `false` | Idle at the dashboard, waiting for a launch. Healthy. |
| `false` | `false` | Mid-handoff between two processes. Recheck in a second. |
| `false` | `true` | The broker gave up restarting PCSX2. Needs intervention. |

The last row usually means a GPU or renderer failure, which will be visible in `PCSX2_LOG_PATH`.

---

### `POST /launch`

Kills any running game and launches a new ROM. Returns immediately; launch runs in a background thread.

```json
{ "rom_path": "/romm/library/ps2/game.chd", "load_slot": 3 }
```

- `rom_path` must exist and be under `ROM_ROOT`. `rom_name` (as shown in `/status`) is not read from the request; it is always derived from `rom_path`'s filename stem.
- `rom_path` may be a **directory**, for libraries laid out one game per folder (`roms/ps2/Jak 3/Jak 3.iso`). RomM addresses such a game by its folder, so the broker looks inside for the disc image: the folder itself first, then one level down for per-disc subfolders. Candidates are ranked by format (`.chd`, `.iso`, `.cso`, `.zso`, `.gz`, `.mdf`, `.dump`, `.bin`, `.elf`) and then by name, so a multi-disc set boots disc 1. Dot-files are skipped, and a symlink pointing outside `ROM_ROOT` is never chosen. The resolved file is what `/status` and the response body report.
- `load_slot` (optional, 1–10) resumes from a save state. Once the game VM reports running (checked via PINE `EMU_STATUS`, up to `RESUME_LOAD_WAIT`, plus a `RESUME_LOAD_SETTLE` grace period), the broker loads that slot. Push the state file with `PUT /state-file` before launching.
- Returns `409` if a save is in progress, a launch is already in progress, or a `/memory-card` replace is in flight
- Returns `400` if `rom_path` is missing or outside `ROM_ROOT`, or `load_slot` is not an integer 1–10
- Returns `422` if `rom_path` does not exist, or if it is a directory with no bootable file inside. The second case reports the accepted extensions in an `extensions` field, so callers never need their own copy of the list

```json
{ "status": "launching", "rom_path": "/romm/library/ps2/game.chd" }
```

---

### `DELETE /launch`

Stops the current game and returns PCSX2 to dashboard mode. Runs in background.

- Returns `409` if a launch is already in progress

```json
{ "status": "resetting" }
```

---

### `POST /save-and-exit`

Saves the current game state via PINE (PCSX2's native IPC socket; falls back to an xdotool F-key press if PINE is unreachable), kills PCSX2, then relaunches the dashboard.

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

Saves the current game to a slot without exiting, via PINE (falling back to an xdotool F-key press if PINE is unreachable). The save runs in the background, so a `200` means the save was dispatched, not that the write finished. `/status` does not expose a `save_in_progress` field. To confirm the write completed, call `GET /state-file` for the same slot: it blocks until any in-flight save finishes (up to `STATE_GET_WAIT`) rather than serving a stale or half-written file. A second `/save-state` call while one is still running gets `409`.

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

Loads a save state into the running game via PINE (falling back to xdotool F-key delivery if PINE is unreachable). This one blocks until the load is dispatched.

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
- Returns `503` if the load could not be delivered over PINE or xdotool (the broker itself is fine)

---

### `GET /state-file?slot=N`

Serves the newest `.p2s` state file for the slot as raw bytes, for RomM's centralized save-state sync. If a save is in flight the response blocks until the write completes (up to `STATE_GET_WAIT`, default 30 s), so a GET fired right after `/save-state` always carries the finished file.

| Query param | Default | Description |
|---|---|---|
| `slot` | `0` | Save state slot (1–10); `0` resolves to `SAVE_SLOT` |

- Response body is `application/octet-stream`; the emulator's own filename is echoed in the `X-State-Filename` header
- Returns `404` if no state file exists for the slot
- Returns `400` if `slot` is not an integer 0–10
- Returns `503` if a save is still in flight after `STATE_GET_WAIT`. Retry rather than accept a half-written file
- Returns `413` if the state file exceeds 256 MB
- Returns `500` if the state file can't be read (filesystem error)

---

### `PUT /state-file?filename=NAME`

Writes a state file into the sstates directory, used by RomM to hydrate a freshly claimed container with the user's stored states. `NAME` is the filename a previous GET returned, written back verbatim so PCSX2 recognises the slot.

- Body is the raw file content (`Content-Length` required, max 256 MB)
- `NAME` must be a bare `.p2s` basename that doesn't start with a dot; path components are rejected
- The write is atomic (temp file + rename) and the file is chowned to `abc` so PCSX2 can overwrite the slot later

```json
{ "status": "ok", "filename": "SLUS-12345 (ABCD1234).03.p2s" }
```

- Returns `400` if `filename` is missing, hidden, not a `.p2s` basename, or `Content-Length` is missing/invalid or the body is truncated
- Returns `413` if the body exceeds 256 MB
- Returns `500` if the write fails (filesystem error)

---

### `GET /save-file`

Zips every in-game save (memory-card file under `memcards/`) modified since the last game launch, for RomM's end-of-session save pull. The baseline survives game exit, so the pull can happen after `/save-and-exit`.

- Response body is `application/zip`; a suggested name is echoed in `X-Save-Filename`
- Files still being written are skipped; their count is reported in `X-Save-Skipped-Unstable`
- Returns `404 {"error": "no game has been launched"}` if the broker has never launched a ROM this session
- Returns `404 {"error": "no save changes since last launch"}` when nothing under `memcards/` has changed since the last game launch. This is the expected steady-state response between saves, not a failure. Poll again after the player saves
- Returns `413` if the changed set exceeds 256 MB
- Returns `503` if every changed file was mid-write. Retry shortly

---

### `PUT /save-file`

Restores a previously pulled save archive into the data dir, used to hydrate a container before launch. Files on disk newer than their archive member are skipped (last-write-wins), so a restore can never roll back saves made since the archive was taken.

- Body is the raw zip (`Content-Length` required, max 256 MB)
- Archive members must live under `memcards/`; anything else is rejected
- Returns `400` for a missing/empty/truncated body, a bad archive, or a member outside the allowed subtree
- Returns `413` if the body exceeds 256 MB
- Returns `500` if some members could not be written. The response carries `written`, `skipped` and `failed` counts, and retrying the same archive is safe (idempotent)

```json
{ "status": "ok", "written": 3, "skipped": 1 }
```

---

### `GET /memory-card`

Serves the **entire** Slot-1 folder memory card (superblock plus every game folder) as one zip, member paths relative to the card root so the image is host- and card-name-independent. This is the per-user card evacuation for pooled containers; Slot 2 is never touched.

- Response is `application/zip` with `X-Memory-Card-Slot: 1`
- Returns `404` with header `X-Memory-Card: absent` if no folder card exists in slot 1. The header distinguishes "genuinely empty" from a missing endpoint, which must not be treated as safe to wipe
- Returns `409` if slot 1 holds a File (`.ps2`) card, since whole-card sync requires a Folder card, or if another card operation is in progress

---

### `PUT /memory-card`

Wipes the Slot-1 folder card and lays down the pulled card image. There is no per-file merge; a full replace is what guarantees isolation between users of a pooled container. Extraction goes to a staging dir swapped atomically over the live card, so a mid-way failure never leaves a half-wiped card.

- Body is the raw zip (`Content-Length` required, max 256 MB)
- Refused while a game is running or a launch is in flight. PCSX2 holds the card open, and replacing it mid-game corrupts it, so hydrate before launch or after exit. `POST /launch` is refused for the duration, so a launch cannot slip in mid-replace.
- Dashboard crash recovery is *not* blocked during a replace: the gameless dashboard never opens a memory card, and the swap is atomic, so a relaunch mid-hydrate sees either the old card or the new one
- Returns `409` if a game is running, a launch is in progress, or another card operation is in progress
- Returns `400` for a missing/empty/truncated body, a bad archive, a member escaping the card dir, when no Slot-1 card is configured, or when Slot 1 already holds a File (`.ps2`) card instead of a Folder card
- Returns `413` if the body exceeds 256 MB

```json
{ "status": "ok", "written": 42 }
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

`mute` in the response is read back from PulseAudio after the change, not just echoed from the request.

- Returns `500` if `pactl` fails (PulseAudio not ready)

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

Expected startup (PCSX2 is launched into the dashboard before the HTTP server starts listening):
```
14:20:15 [broker] INFO Broker starting — waiting for desktop X display...
14:20:16 [broker] INFO Desktop ready on DISPLAY=:1
14:20:16 [broker] INFO Launching PCSX2 (rom=dashboard)
14:20:17 [broker] INFO PCSX2 launched (PID 42, initial save slot 1)
14:20:17 [broker] INFO ROM broker listening on port 8000
14:20:17 [broker] INFO Shared secret auth enabled
```

Expected on game launch:
```
14:22:10 [broker] INFO Stopping PCSX2 (PID 42)...
14:22:10 [broker] INFO Launching PCSX2 (rom=/romm/library/ps2/game.chd)
14:22:11 [broker] INFO PCSX2 launched (PID 123, initial save slot 1)
```

Expected on save-and-exit (PINE is tried first; xdotool only appears if PINE is unreachable):
```
14:25:45 [broker] INFO PINE: save state accepted (slot 10) — waiting for write (max 20.0s)
14:25:45 [broker] INFO Save: state write complete — SLUS-12345 (ABCD1234).10.p2s (285212 bytes) in 0.3s
14:25:45 [broker] INFO Stopping PCSX2 (PID 123)...
14:25:48 [broker] INFO Launching PCSX2 (rom=dashboard)
14:25:49 [broker] INFO PCSX2 launched (PID 456, initial save slot 1)
```

---

## Development

The broker's logic is covered by a stdlib `unittest` suite. There is no pytest and nothing to install, which matters because the container has no pip. Run it from the repo root:

```bash
python3 -m unittest discover -s tests -v
```

`tests/` is excluded from the image via `.dockerignore`, so it never ships in a build.

The suite covers the save-file and memory-card archive handling (round trips, the last-write-wins mtime guard, path-traversal and subtree rejection, File-card refusal), `PCSX2.ini` patching and parsing, ROM path validation, and the session lifecycle state machine (crash-loop limiter, launch/card-op claims, deferred `load_slot` generation checks).

Anything needing a real X display, PINE socket, or `pcsx2-qt` process is deliberately out of scope. Those paths are only provable on a running container.

---

## Pinning to a Version

```yaml
# Exact version, never changes
- DOCKER_MODS=ghcr.io/loneangelfayt/pcsx2-romm-integration-mod:v1.2.0

# Minor pin, gets patches only
- DOCKER_MODS=ghcr.io/loneangelfayt/pcsx2-romm-integration-mod:v1.2

# Always latest release
- DOCKER_MODS=ghcr.io/loneangelfayt/pcsx2-romm-integration-mod:latest
```

Available versions: [Packages page](https://github.com/LoneAngelFayt/pcsx2-romm-integration/pkgs/container/pcsx2-romm-integration-mod)

---

## Roadmap

| Feature | Status | Notes |
|---|---|---|
| Game launching via RomM | Done | `POST /launch` |
| Save state on exit | Done | `POST /save-and-exit`, over PINE with an xdotool F-key fallback, slot 10 by default |
| Return to dashboard on exit | Done | Automatic after any exit path |
| Volume control | Done | `POST /volume` and `POST /mute` via `pactl` |
| Manual save state (no exit) | Done | `POST /save-state` with slot selection |
| Manual load state | Done | `POST /load-state` with slot selection |
| RomM save state export/import | Done | `GET`/`PUT /state-file`. RomM pulls states into the library and hydrates slots on claim |
| Resume from a save state on launch | Done | `load_slot` on `POST /launch` |
| In-game save sync | Done | `GET`/`PUT /save-file`. Delta pull of changed memory-card saves, restored last-write-wins |
| Whole memory-card sync | Done | `GET`/`PUT /memory-card`. Full Slot-1 folder card evacuate and hydrate for pooled containers |

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
- Saves go over PINE first (`docker logs pcsx2 | grep PINE`); xdotool is only used as a fallback when PINE is unreachable, so no `xdotool` log lines is normal on a healthy container
- If you do see `PINE unavailable — falling back to xdotool`, check broker logs for xdotool output: `docker logs pcsx2 | grep xdotool`
- The xdotool window-search success message (`xdotool: found window ...`) is logged at DEBUG, so set `BROKER_LOG_LEVEL=DEBUG` to see it. The failure message (`PCSX2 window not found`) is logged at WARNING and shows by default
- If the window isn't found, confirm `xdotool` is installed: `docker exec pcsx2 which xdotool`
- Increase `PINE_WAIT` (default 20s) if saves are slow: `PINE_WAIT=30.0`
- Save files land at `SSTATE_DIR` (`/config/.config/PCSX2/sstates` by default): `docker exec pcsx2 ls /config/.config/PCSX2/sstates`

**PCSX2 crashes immediately after game launch**
- Check emulog for GPU or BIOS errors
- Check `PCSX2_LOG_PATH` (`/config/pcsx2-qt.log` by default) for renderer and Vulkan errors, which is where a shader compilation failure shows up
- Ensure the game file is not corrupted: `.chd`, `.iso`, `.bin/.cue` are all supported

**Nothing restarts after repeated crashes**
- Check `GET /status`. If `relaunch_abandoned` is `true`, PCSX2 exited within 5 seconds three times in a row and the broker deliberately stopped restarting it rather than respawning a broken process forever
- Fix the underlying failure first (usually GPU or BIOS, visible in `PCSX2_LOG_PATH`), then `POST /launch` to recover. A successful launch clears the flag

**A game "launches" but never boots**
- An unbootable or corrupt ROM does not crash PCSX2. The process stays alive on an error dialog, so `/status` reports an active session and the crash-loop limiter never engages
- Confirm the file is a valid disc image and that BIOS files are present in `/config/bios`

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
