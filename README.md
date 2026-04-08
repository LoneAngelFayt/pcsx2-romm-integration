# pcsx2-romm-integration

A [LinuxServer Docker Mod](https://docs.linuxserver.io/general/container-customization) that integrates [PCSX2](https://pcsx2.net/) with [RomM](https://github.com/rommapp/romm) — launching PS2 games on demand from RomM's web UI and streaming the output back via Selkies.

---

## What It Does

This mod installs a small HTTP broker inside the [linuxserver/pcsx2](https://docs.linuxserver.io/images/docker-pcsx2/) container that:

1. Exposes an API on port 8000 so RomM can request game launches
2. Manages the PCSX2 process lifecycle (start, stop, game switching, dashboard mode)
3. Saves game state via PCSX2's PINE IPC before exit
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
| `PINE_SOCKET` | `/config/.XDG/pcsx2.sock` | Path to PCSX2's PINE IPC socket. Auto-discovered if not present at this path. |
| `PINE_TIMEOUT` | `5.0` | Timeout (seconds) for connecting to and sending via the PINE socket. |
| `PINE_WAIT` | `3.0` | Time (seconds) to wait after sending a PINE save command before killing PCSX2, giving the write time to complete. |
| `SAVE_SLOT` | `0` | Default save state slot (0–9) for `/save-and-exit` when no `slot` is specified. |
| `SSTATE_DIR` | `/config/.config/PCSX2/sstates` | Where PCSX2 writes save state files. Currently informational — used by a planned RomM export/import feature. |

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
{ "rom_path": "/romm/library/ps2/game.chd", "rom_name": "Game Title" }
```

- `rom_path` must exist and be under `ROM_ROOT`
- Returns `409` if a save is in progress
- Returns `400` if `rom_path` is missing or outside `ROM_ROOT`
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

Saves the current game state via PINE IPC, kills PCSX2, then relaunches the dashboard.

**Request body:**
```json
{ "slot": 0, "wait": true }
```

| Field | Default | Description |
|---|---|---|
| `slot` | `SAVE_SLOT` env var | Save state slot (0–9) |
| `wait` | `true` | `true` = blocking (responds after save+kill complete); `false` = fire-and-forget (responds immediately, save+kill in background) |

**`wait=true` response:**
```json
{ "status": "ok", "saved": true, "slot": 0 }
```

**`wait=false` response:**
```json
{ "status": "queued", "slot": 0 }
```

- Returns `409` if no game is running or if a save is already in progress
- Returns `400` if `slot` is not an integer 0–9

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
14:25:45 [broker] INFO PINE: save command sent (slot 0) — waiting 3.0s for write
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
| Save state on exit (PINE IPC) | ✅ Done | `POST /save-and-exit` |
| Return to dashboard on exit | ✅ Done | Automatic after any exit path |
| Manual save state (no exit) | 🔜 Planned | `POST /save-state` with slot selection |
| Manual load state | 🔜 Planned | `POST /load-state` with slot selection |
| Volume control | 🔜 Planned | Via `pactl` or PINE; wire to RomM volume slider |
| RomM save state export/import | 🔜 Planned | Sync `.p2s` files from `SSTATE_DIR` to/from RomM library |

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
- Check logs for the actual PINE socket path: `docker logs pcsx2 | grep PINE`
- Verify PINE is enabled: `docker exec pcsx2 grep EnablePINE /config/.config/PCSX2/inis/PCSX2.ini`
- Increase `PINE_WAIT` (default 3s) if save files are incomplete: `PINE_WAIT=6.0`

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
