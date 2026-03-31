# pcsx2-broker-mod

A [LinuxServer Docker Mod](https://docs.linuxserver.io/general/container-customization) that installs and runs `broker.py` inside the [linuxserver/pcsx2](https://docs.linuxserver.io/images/docker-pcsx2/) container for RomM integration.

---

## What It Does

On every container start, this mod:

1. Checks for Python 3 and installs it if missing
2. Copies `broker.py` to `/root/broker.py` and makes it executable
3. Starts `broker.py` as a managed background service via s6-overlay
4. Automatically restarts `broker.py` if it crashes

---

## Requirements

- A running [linuxserver/pcsx2](https://docs.linuxserver.io/images/docker-pcsx2/) container
- Docker Compose (or equivalent)
- Internet access from the container (for Python install if not present)

---

## Installation

Add the `DOCKER_MODS` environment variable to your PCSX2 container:
```yaml
services:
  pcsx2:
    image: lscr.io/linuxserver/pcsx2:latest
    container_name: pcsx2
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=America/Chicago
      - DOCKER_MODS=ghcr.io/loneangelfayt/pcsx2-broker-mod:latest
    # ... rest of your config
```

Then recreate the container:
```bash
docker compose up -d --force-recreate
```

That's it. The mod will be pulled and applied automatically on every container start.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `BROKER_SECRET` | Recommended | *(none)* | Shared secret for authenticating launch requests. Sent as the `X-Broker-Secret` header. If unset, the broker accepts unauthenticated requests from anyone with network access. |
| `BROKER_PORT` | No | `8000` | Port the broker HTTP server listens on. Only set this if `8000` conflicts with another service. |
| `DISPLAY` | No | Auto-detected | X display to launch PCSX2 on. Auto-detected from `/tmp/.X11-unix/` — only override if auto-detection fails. |

### Recommended `docker-compose.yml`
```yaml
services:
  pcsx2:
    image: lscr.io/linuxserver/pcsx2:latest
    container_name: pcsx2
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=America/Chicago
      - DOCKER_MODS=ghcr.io/loneangelfahyt/pcsx2-broker-mod:latest
      - BROKER_SECRET=your_secret_here   # recommended — leave blank to disable auth
    ports:
      - 8000:8000   # broker API
    # ... rest of your config
```

> **Note:** `BROKER_SECRET` is optional but strongly recommended. Without it, anyone on your network can send launch commands to the broker.

---

## API Reference

The broker exposes a small HTTP API on `BROKER_PORT`.

### `GET /health`
Returns `200 OK` if the broker is running.

### `GET /status`
Returns the currently active session, or `{"active": false}` if idle.
```json
{
  "active": true,
  "rom_path": "/data/roms/ps2/game.iso",
  "rom_name": "game",
  "started_at": "2024-01-01T00:00:00Z"
}
```

### `POST /launch`
Kills any running game and launches a new ROM.

**Headers:**
```
Content-Type: application/json
X-Broker-Secret: your_secret_here
```

**Body:**
```json
{
  "rom_path": "/data/roms/ps2/game.iso",
  "rom_name": "Game Title"
}
```

> `rom_path` must be the path **inside the container**. Make sure your ROMs volume is mounted at the same path in both the PCSX2 container and whatever is calling the broker (e.g. RomM).

### `DELETE /launch`
Stops the current game and returns PCSX2 to the dashboard.

**Headers:**
```
X-Broker-Secret: your_secret_here
```


## Pinning to a Specific Version

By default `:latest` always pulls the newest release. If you want a stable pin that never changes:
```yaml
# Pinned to exact version — never changes
- DOCKER_MODS=ghcr.io/loneangelfahyt/pcsx2-broker-mod:v1.0.0

# Pinned to minor — gets patches but not breaking changes
- DOCKER_MODS=ghcr.io/loneangelfahyt/pcsx2-broker-mod:v1.0

# Always latest release
- DOCKER_MODS=ghcr.io/loneangelfahyt/pcsx2-broker-mod:latest
```

Available versions can be found on the [Packages page](https://github.com/LoneAngelFayt/pcsx2-broker-mod/pkgs/container/pcsx2-broker-mod).

---

## Verifying It's Running

Check the container logs for broker mod output:
```bash
docker logs pcsx2 | grep broker
```

You should see:
```
[broker-mod] Checking for Python3...
[broker-mod] Python3 already installed: Python 3.x.x
[broker-mod] Setting broker.py executable...
[broker-mod] Starting broker.py...
```

---

## Versions

This mod follows [Semantic Versioning](https://semver.org/):

| Change type | Example commit | Version bump |
|---|---|---|
| Bug fix or tweak | `fix: broker reconnect timeout` | `v1.0.0 → v1.0.1` |
| New feature | `feat: add romm auth header` | `v1.0.1 → v1.1.0` |
| Breaking change | `feat!: rewrite broker protocol` | `v1.1.0 → v2.0.0` |

Releases are created automatically when changes are merged to `main`. See [Releases](https://github.com/LoneAngelFayt/pcsx2-broker-mod/releases) for the full changelog.

---

## Troubleshooting

**Mod doesn't appear to be applying**
Make sure `DOCKER_MODS` is set correctly and the image tag exists. Run `docker compose up` (not `-d`) to see full output during startup.

**Python install fails on startup**
The container needs outbound internet access. Check your network config or firewall rules.

**broker.py crashes immediately**
Check logs with `docker logs pcsx2`. The s6 supervisor will attempt to restart it — repeated crashes will show up in the log output.


---

## License

GPLv3
