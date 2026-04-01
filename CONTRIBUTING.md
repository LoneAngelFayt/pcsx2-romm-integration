# Contributing

## How It Works

This is a [LinuxServer Docker Mod](https://docs.linuxserver.io/general/container-customization). The entire mod is a directory tree under `root/` that gets overlaid onto the container filesystem at runtime. The `Dockerfile` is just:

```dockerfile
FROM scratch
COPY root/ /
```

### Key Files

| File | Purpose |
|---|---|
| `root/custom-cont-init.d/50-broker-dirs` | Runs before any services start. Creates config dirs, BIOS symlink, and fixes XDG permissions. |
| `root/etc/s6-overlay/s6-rc.d/svc-broker/run` | s6 service that patches `PCSX2.ini` (PINE IPC, BIOS path, absolute save paths) and writes the labwc autostart, then starts `broker.py`. |
| `root/root/broker.py` | HTTP broker. Exposes the API used by RomM to launch/control games. |
| `root/usr/local/bin/pcsx2-launcher.sh` | Called by the labwc autostart loop. Re-patches the INI (PCSX2 resets it on exit), reads `/tmp/pcsx2-rom`, and execs `pcsx2-qt`. |
| `root/defaults/startwm_wayland.sh` | Replaces the base image's DE launcher. Starts labwc with the autostart script. |
| `root/usr/local/bin/xkbcomp` | Wrapper that silences harmless XF86 keysym warnings from Xwayland keyboard init. |

### Launch Flow

1. RomM calls `POST /launch` with a ROM path.
2. `broker.py` kills any running PCSX2 instance and waits for it to exit.
3. It writes the ROM path to `/tmp/pcsx2-rom`.
4. The labwc autostart loop (running `pcsx2-launcher.sh` in a `while true` loop) picks up the signal file and execs `pcsx2-qt -batch -fullscreen <rom>`.
5. On release (`DELETE /launch` or heartbeat timeout), the broker triggers a PINE save state, deletes `/tmp/pcsx2-rom`, and kills PCSX2. The autostart loop restarts it in dashboard mode.

### PINE IPC

PCSX2 exposes a Unix socket at `/config/.XDG/pcsx2.sock` when `EnablePINE = true` is set in `PCSX2.ini`. The broker uses this for save/load state operations. The socket only exists while a game is running.

Relevant opcodes used:
- `9` — save state
- `10` — load state

### s6 Init Order

```
init-mods-end
  → init-custom-files   (runs /custom-cont-init.d/50-broker-dirs)
    → init-services
      → svc-de          (labwc → autostart → pcsx2-launcher.sh → pcsx2-qt)
      → svc-broker      (broker.py)
```

`svc-broker` and `svc-de` start in parallel after `init-services`. The broker waits up to 60s for `PCSX2.ini` to appear before patching it.

---

## Development

### Testing Locally

Build the image and apply it to a running PCSX2 container:

```bash
# Build
docker build -t pcsx2-mod-local .

# Apply via DOCKER_MODS (requires the mod image to be accessible)
# For local testing, copy files directly into a running container:
docker cp root/root/broker.py pcsx2:/root/broker.py
docker exec pcsx2 python3 /root/broker.py
```

### Checking Logs

```bash
# All broker output
docker logs pcsx2 2>&1 | grep -E "\[broker"

# PCSX2 emulator log
docker exec pcsx2 tail -f /config/.config/PCSX2/logs/emulog.txt
```

### Testing Endpoints

```bash
SECRET=your_secret_here

curl -s http://localhost:8000/health
curl -s http://localhost:8000/status

curl -s -X POST http://localhost:8000/launch \
  -H "Content-Type: application/json" \
  -H "X-Broker-Secret: $SECRET" \
  -d '{"rom_path": "/romm/library/roms/ps2/game.chd", "rom_name": "Game"}'

curl -s -X DELETE http://localhost:8000/launch \
  -H "X-Broker-Secret: $SECRET"
```

---

## Releases

Releases are created automatically by [semantic-release](https://semantic-release.gitbook.io/) when commits are merged to `main`. The version bump is determined by commit prefixes:

| Prefix | Example | Bump |
|---|---|---|
| `fix:` | `fix: handle missing PINE socket` | patch |
| `feat:` | `feat: add POST /screenshot` | minor |
| `feat!:` / `BREAKING CHANGE` | `feat!: change auth header name` | major |

The built image is pushed to `ghcr.io/loneangelfayt/pcsx2-romm-integration-mod`.
