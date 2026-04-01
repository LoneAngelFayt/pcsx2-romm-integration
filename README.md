# pcsx2-romm-integration-mod

A [LinuxServer Docker Mod](https://docs.linuxserver.io/general/container-customization) that adds a ROM launch broker to the [linuxserver/pcsx2](https://docs.linuxserver.io/images/docker-pcsx2/) container, enabling RomM (or any HTTP client) to launch and control games remotely.

---

## Requirements

- A running [linuxserver/pcsx2](https://docs.linuxserver.io/images/docker-pcsx2/) container
- Docker Compose (or equivalent)
- A PS2 BIOS mounted at `/config/bios` inside the container
- ROMs volume mounted at the same path in both the PCSX2 and RomM containers

---

## Installation

Add the mod to your PCSX2 container's environment:

```yaml
environment:
  - DOCKER_MODS=ghcr.io/loneangelfayt/pcsx2-romm-integration-mod:latest
```

Then recreate the container:

```bash
docker compose up -d --force-recreate
```

The mod is pulled and applied automatically on every container start.

---

## Example `docker-compose.yml`

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
    ports:
      - 3000:3000   # PCSX2 web UI
      - 3001:3001   # PCSX2 web UI (HTTPS)
      - 8000:8000   # broker API
    volumes:
      - /path/to/config:/config
      - /path/to/roms:/romm/library/roms  # must match RomM's mount path
      - /path/to/bios:/config/bios
```

> **Note:** `BROKER_SECRET` is optional but strongly recommended. Without it, anyone with network access to port 8000 can send commands to the broker.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `BROKER_SECRET` | Recommended | *(none)* | Shared secret sent as the `X-Broker-Secret` header. If unset, the broker accepts unauthenticated requests. |
| `BROKER_PORT` | No | `8000` | Port the broker HTTP server listens on. |
| `BROKER_SAVE_SLOT` | No | `0` | Save state slot used for auto-save on session release (0–9). |
| `BROKER_HEARTBEAT_TIMEOUT` | No | `120` | Seconds without a heartbeat before the session is auto-released. Set to `0` to disable. |

---

## API Reference

All endpoints that modify state require the `X-Broker-Secret` header if `BROKER_SECRET` is set.

### `GET /health`
Returns `200 OK` if the broker is running.

### `GET /status`
Returns the active session or `{"active": false}` if idle.
```json
{
  "active": true,
  "rom_path": "/romm/library/roms/ps2/game.chd",
  "rom_name": "Game Title",
  "started_at": "2024-01-01T00:00:00Z",
  "paused": false
}
```

### `POST /launch`
Launches a ROM. Kills any running game first.
```json
{ "rom_path": "/romm/library/roms/ps2/game.chd", "rom_name": "Game Title" }
```
> `rom_path` must be the path **inside the container**.

### `DELETE /launch`
Saves state to `BROKER_SAVE_SLOT` then stops the current game.

### `POST /heartbeat`
Resets the heartbeat timer. Call this on an interval (e.g. every 30s) to keep the session alive while the user is actively playing.

### `POST /restart`
Kills and relaunches the current ROM without changing the session.

### `POST /pause`
Toggles pause. First call pauses, second resumes.

### `POST /savestate`
Triggers a save state. Optional `slot` (0–9) defaults to `BROKER_SAVE_SLOT`.
```json
{ "slot": 2 }
```

### `POST /loadstate`
Loads a save state. Optional `slot` (0–9) defaults to `BROKER_SAVE_SLOT`.
```json
{ "slot": 2 }
```

### `POST /screenshot`
Triggers a screenshot. Saved to `/config/.config/PCSX2/snaps/` inside the container.

### `POST /volume`
Sets the PCSX2 audio volume. `level` is 0–100.
```json
{ "level": 75 }
```

### `GET /savefile?card=1`
Downloads the memory card file (`Mcd001.ps2` or `Mcd002.ps2`) as a binary attachment. Use `card=1` or `card=2`.

---

## Verifying It's Running

```bash
docker logs pcsx2 | grep broker
```

On a healthy start you'll see:
```
[broker-mod] Creating PCSX2 config directories...
[broker-mod] Existing config found — applying patches...
[broker-mod] Starting broker.py...
[broker] INFO ROM broker listening on port 8000
```

You can also hit the health endpoint:
```bash
curl http://localhost:8000/health
# {"status": "ok"}
```

---

## Pinning to a Specific Version

```yaml
# Always latest
- DOCKER_MODS=ghcr.io/loneangelfayt/pcsx2-romm-integration-mod:latest

# Pinned to a specific release
- DOCKER_MODS=ghcr.io/loneangelfayt/pcsx2-romm-integration-mod:v1.2.0
```

Available versions: [Packages page](https://github.com/LoneAngelFayt/pcsx2-romm-integration-mod/pkgs/container/pcsx2-romm-integration-mod)

---

## Troubleshooting

**Mod doesn't apply**
Ensure `DOCKER_MODS` is set correctly and the image tag exists. Run `docker compose up` (without `-d`) to see full startup output.

**ROM launches but shows game list instead of the game**
Confirm the ROM path is accessible inside the PCSX2 container and that the volume is mounted at the same path in both containers.

**BIOS not found**
Ensure your BIOS files are mounted at `/config/bios` inside the container. The broker logs a warning at startup if no BIOS files are detected.

**Save states not persisting**
Check that `/config/.config/PCSX2/sstates` is writable. The broker patches PCSX2.ini on every start to pin this to an absolute path.

**Broker crashes repeatedly**
Check `docker logs pcsx2` — the s6 supervisor will restart it automatically. Repeated crashes will appear in the log.

---

## Roadmap

- **Save file import** (`POST /savefile`) — restore a memory card from a previously exported file.
- **Per-game save file endpoints** — serve and receive individual game saves rather than full memory card images.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

GPLv3
