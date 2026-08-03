# pcsx2-romm-integration

A [LinuxServer Docker Mod](https://docs.linuxserver.io/general/container-customization) that lets [RomM](https://github.com/rommapp/romm) drive [PCSX2](https://pcsx2.net/). Pick a PS2 game in the RomM web UI, and it boots in the PCSX2 container and streams back to your browser.

## What the broker actually is

The mod drops a single Python file into the [linuxserver/pcsx2](https://docs.linuxserver.io/images/docker-pcsx2/) container and runs it as an s6 service. It is a small HTTP server, stdlib only, listening on port 8000. RomM talks to it; it talks to PCSX2.

That is the whole design. RomM never touches the emulator directly, because there is no way to. PCSX2 is a desktop app with no remote control API, so something inside the container has to stand in for a person sitting at the keyboard. That is the broker's job:

```
browser ──── Selkies (WebRTC video) ────┐
                                        │
RomM backend ──── HTTP ──── broker ──── pcsx2-qt
                                │
                                ├── PINE socket    save/load state, VM status
                                ├── xdotool        F-keys, when PINE won't answer
                                ├── PCSX2.ini      patched before every launch
                                └── pactl          volume and mute
```

Four things are worth knowing before you read the API:

**PCSX2 is always running.** There is no "stopped" state. When no game is loaded the broker keeps pcsx2-qt alive on its own dashboard, so the stream always shows something and a launch is a process swap rather than a cold start. This is why `/status` reports `active: true` on an idle container (see [Session state](#session-state)).

**Saves go over PINE first.** [PINE](https://github.com/PCSX2/pcsx2/blob/master/pcsx2/PINE.h) is PCSX2's own IPC socket, and it carries a slot number directly, so the broker asks for slot 7 and gets slot 7. When PINE doesn't answer, the broker falls back to synthesising F-key presses with xdotool, which means cycling the slot selector and hoping the window has focus. PINE working is the normal case. Seeing xdotool in the logs is a hint something is wrong.

**Two kinds of save, two kinds of sync.** Save *states* are whole-VM snapshots (`.p2s`), handled by `/state-file`. In-game saves live on the emulated memory card, handled by `/save-file` and `/memory-card`. They are unrelated mechanisms and RomM syncs them separately. The memory card sync has a [setup requirement](#memory-card-setup).

**It supervises itself, up to a point.** s6 restarts the broker if it dies, and the broker restarts pcsx2-qt if it dies. If PCSX2 dies three times inside five seconds each, the broker stops trying, because respawning a broken renderer forever helps nobody. That state is visible as `relaunch_abandoned`.

## Quick start

Add the mod to your PCSX2 container:

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
      - STREAM_GATE=off        # see Security; 'token' once RomM sends the token
    ports:
      - 8000:8000   # broker API
    volumes:
      - ./config:/config
      - /mnt/roms:/romm/library   # must match ROM_ROOT, shared with RomM
```

```bash
docker compose up -d --force-recreate pcsx2
```

Then point RomM at it in `config.yml`:

```yaml
streaming:
  enabled: true
  containers:
    - platform: ps2
      host: "https://192.168.x.x:3001"        # browser-facing Selkies UI
      broker_host: "http://pcsx2:8000"        # optional, derived from host if omitted
      label: "PCSX2"
```

The `platform` slug has to match what your PS2 ROMs use in RomM. The ROM volume has to be mounted at the *same path* in both containers: if RomM sees `/romm/library/ps2/game.chd`, PCSX2 must see it there too, or every launch will 422.

Confirm it came up:

```bash
docker logs pcsx2 | grep broker
```

```
[broker] INFO Desktop ready on DISPLAY=:1
[broker] INFO Launching PCSX2 (rom=dashboard)
[broker] INFO PCSX2 launched (PID 42, initial save slot 1)
[broker] INFO ROM broker listening on port 8000
[broker] INFO Shared secret auth enabled
```

PCSX2 is launched into the dashboard *before* the HTTP server starts listening, so if you can reach `/health` the emulator is already up.

## Memory card setup

**The PCSX2 memory card in Slot 1 must be `Folder` type, not `File`.** In-game save sync does not work otherwise, and `/memory-card` will refuse with a `409`.

A `File` card is one 8 MB blob holding every game's saves together, so there is no way to sync or isolate a single game out of it. A `Folder` card gives each game its own directory keyed by serial (`BASLUS-20267DOTHACK/`), which is what makes per-game sync possible at all.

Set it under **Settings → Memory Cards**: create a card of type `Folder` and assign it to Slot 1. Slot 1 is the only slot synced; Slot 2 is never touched. You should end up with this in `inis/PCSX2.ini`:

```ini
[MemoryCards]
Slot1_Enable = true
Slot1_Filename = my-folder-card.ps2   # a directory on disk, despite the name
```

Save states don't care about any of this. The requirement is only for memory-card saves.

## Configuration

`BROKER_SECRET` is the only one you really need to set. Everything else has a working default.

| Variable | Default | What it does |
|---|---|---|
| `BROKER_SECRET` | *(none)* | Shared secret, sent as `X-Broker-Secret`. Unset means every request is accepted. Debug-only, see [Security](#security). |
| `BROKER_PORT` | `8000` | Port the broker listens on. |
| `ROM_ROOT` | `/romm/library` | Where ROMs are mounted. A `rom_path` outside this is rejected. |
| `SAVE_SLOT` | `10` | Slot `/save-and-exit` uses when none is given. 10 as autosave leaves 1-9 for the player. |
| `PINE_WAIT` | `20.0` | Seconds to poll for a state write to land after a save is accepted. Stops early once the file appears. Raise it for slow disks. |
| `SSTATE_DIR` | `/config/.config/PCSX2/sstates` | Where PCSX2 writes `.p2s` files, and where `/state-file` reads and writes them. |
| `SAVE_DATA_ROOT` | `/config/.config/PCSX2` | PCSX2 data dir for `/save-file` and `/memory-card`. Only `memcards/` under it is synced. |
| `STATE_GET_WAIT` | `30.0` | How long `GET /state-file` waits for an in-flight save before giving up. |
| `RESUME_LOAD_WAIT` | `90.0` | How long a `load_slot` launch waits for the game VM to report running. |
| `RESUME_LOAD_SETTLE` | `3.0` | Grace period after the VM reports running, before the deferred state load fires. |
| `BROKER_INITIAL_SLOT` | `1` | Slot the broker assumes PCSX2 booted on. PCSX2 never reports its real one, so this is the seed the xdotool cycling tracks from. Only matters for xdotool cycling. |
| `PCSX2_LOG_PATH` | `/config/pcsx2-qt.log` | Captures pcsx2-qt stdout and stderr. Renderer and Vulkan failures show up here. Appended across launches. |
| `BROKER_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. The xdotool window-found messages are DEBUG. |
| `PUID` / `PGID` | `1000` | Standard LinuxServer UID/GID. Also used to chown files the broker writes for PCSX2, which runs as `abc` and has to overwrite them later. |
| `STREAM_TOKEN_TTL` | `43200.0` | Idle seconds before a stream token stops working. Every admitted request slides it forward, so it only fires on a session nobody is watching. Stops an abandoned session leaving the stream gate open forever. |
| `STREAM_TOKEN_GRACE` | `120.0` | How long the previous token keeps working after a relaunch mints a new one, so an already-open tab is not cut off mid-navigation. |
| `STREAM_GATE` | `off` | `token` enforces the stream gate: the desktop on 3000/3001 admits only requests carrying the token `POST /launch` mints. `off` admits everything. Defaults to `off` because released RomM cannot send that token yet, see [Security](#security). |

## API

Everything returns JSON except the raw file and zip bodies. Send `X-Broker-Secret` on every request when `BROKER_SECRET` is set, or get a `403`. `GET /health` is the exception: it never checks the secret, so it works as a container healthcheck.

JSON bodies are capped at 64 KB and file bodies at 256 MB. Anything larger is a `413`; anything that isn't a JSON object is a `400`.

| Endpoint | Does | Notable failures |
|---|---|---|
| `GET /health` | `{"status": "ok"}` if the broker is up | none |
| `GET /status` | Current session, see [below](#session-state) | none |
| `POST /launch` | Boot a ROM. Returns immediately, launch runs in the background | `400` bad or missing `rom_path`, `409` save/launch/card op in flight, `422` path missing or nothing bootable in the folder |
| `DELETE /launch` | Stop the game, back to the dashboard | `409` launch in progress |
| `POST /save-and-exit` | Save state, kill PCSX2, relaunch the dashboard. `wait: false` to fire and forget | `409` no game running or save in progress |
| `POST /save-state` | Save to a slot without exiting. Async, see [below](#saving-is-asynchronous) | `409` no game running or save in progress |
| `POST /load-state` | Load a slot into the running game. Blocks until dispatched | `409` no game, `503` neither PINE nor xdotool got through |
| `GET /state-file?slot=N` | The newest `.p2s` for that slot, raw bytes. Filename echoed in `X-State-Filename` | `404` no such state, `503` a save was still running after `STATE_GET_WAIT` |
| `PUT /state-file?filename=NAME` | Write a state file back, atomically. Pass the filename a GET gave you, verbatim, or PCSX2 won't match it to the slot | `400` not a bare `.p2s` name, or truncated body |
| `GET /save-file` | Zip of memory-card saves changed since the last launch | `404` nothing launched yet, or nothing changed. See [below](#pulling-in-game-saves) |
| `PUT /save-file` | Restore a save zip. Files newer on disk are skipped, so a restore can't roll back newer saves | `400` bad archive or a member outside `memcards/` |
| `GET /memory-card` | The whole Slot-1 folder card as one zip | `404` + `X-Memory-Card: absent` if there is no card, `409` if Slot 1 is a File card |
| `PUT /memory-card` | Replace the whole Slot-1 card. See [below](#replacing-a-memory-card) | `409` game running, launch in flight, or another card op |
| `POST /volume` | `{"level": 0-100}` | `400` out of range, `500` pactl failed |
| `POST /mute` | `{"mute": true}`, or omit `mute` to toggle. The response is read back from PulseAudio, not echoed | `500` pactl failed |
| `POST /cleanup` | Restart Selkies to flush stale gamepad sockets. Use it if controllers go dead | none |

Slots are 1-10 everywhere. `/save-and-exit` and `GET /state-file` also accept `0`, which means "use `SAVE_SLOT`".

### Session state

```json
{
  "active": true,
  "rom_path": "/romm/library/ps2/game.chd",
  "rom_name": "game",
  "started_at": "2026-01-01T00:00:00Z",
  "relaunch_abandoned": false
}
```

`active` only means a pcsx2-qt process is alive, and the idle dashboard is a normal pcsx2-qt process, so an idle container reports `active: true`. **To test whether a game is running, check `rom_path`**, which is `null` on the dashboard. `rom_name` and `started_at` follow it, and all three go `null` whenever `active` is false regardless of what the broker still remembers.

Read the two flags together to tell what a quiet container is doing:

| `active` | `relaunch_abandoned` | Meaning |
|---|---|---|
| `true` | `false` | Idle at the dashboard. Healthy. |
| `false` | `false` | Mid-handoff between two processes. Check again in a second. |
| `false` | `true` | The broker gave up restarting PCSX2. Needs you. |

That last row is almost always a GPU or renderer failure, and `PCSX2_LOG_PATH` will say which.

### Launching

```json
{ "rom_path": "/romm/library/ps2/game.chd", "load_slot": 3 }
```

`rom_name` is never read from the request. It is always derived from the resolved filename.

`rom_path` may be a **directory**, which matters more than it sounds. RomM addresses a folder-organized game by its folder, because `Rom.full_path` is `fs_path/fs_name` and for a multi-file ROM `fs_name` *is* the directory. So a library laid out one game per folder (`roms/ps2/Jak 3/Jak 3.iso`) sends the broker a path PCSX2 cannot boot. The broker looks inside for the disc image: the folder itself first, then one level down for per-disc subfolders, and no deeper. Candidates are ranked by format (`.chd`, `.iso`, `.cso`, `.zso`, `.gz`, `.mdf`, `.dump`, `.bin`, `.elf`) then by name, so a multi-disc set boots disc 1. Dot-files are skipped and a symlink pointing outside `ROM_ROOT` is never chosen. Whatever it resolves to is what `/status` reports.

A folder with nothing bootable in it returns `422` with an `extensions` list, which is a different message from the `422` for a path that doesn't exist. Every broker in this family behaves the same way here.

`load_slot` (1-10) resumes from a state. The broker waits for the VM to report running over PINE, gives it `RESUME_LOAD_SETTLE` seconds to settle, then loads. Push the state file with `PUT /state-file` *before* you launch.

### Saving is asynchronous

`POST /save-state` returns `200` when the save has been *dispatched*, not when the write finished, and `/status` has no `save_in_progress` field to poll. To confirm a save landed, call `GET /state-file` for the same slot: it blocks until any in-flight write completes, up to `STATE_GET_WAIT`, rather than handing you a half-written file. A second `/save-state` while one is running gets a `409`.

State files are named `{SERIAL} ({CRC}).{slot:02d}.p2s`, which is PCSX2's own scheme, not ours. That is why `PUT /state-file` insists on the exact filename a GET returned.

### Pulling in-game saves

`GET /save-file` is a delta pull: it zips only memory-card files modified since the last game launch. The baseline survives the game exiting, so RomM can pull *after* `/save-and-exit`.

`404 {"error": "no save changes since last launch"}` is the normal steady state between saves, not a failure. Files caught mid-write are skipped and counted in `X-Save-Skipped-Unstable`; if *every* changed file was mid-write you get a `503`, and retrying shortly is the right move.

### Replacing a memory card

`PUT /memory-card` wipes the Slot-1 card and lays down the uploaded one. There is no per-file merge, deliberately: a full replace is the only thing that guarantees one user of a pooled container can't see another's saves. Extraction goes to a staging directory that is swapped over the live card atomically, so a failure halfway through can't leave a half-wiped card.

It is refused while a game is running or a launch is in flight, because PCSX2 holds the card open and swapping it mid-game corrupts it. Hydrate before launch or after exit. `POST /launch` is refused for the duration too, so a launch can't slip in mid-replace. Dashboard crash recovery is *not* blocked, since the gameless dashboard never opens a card and the swap is atomic either way.

## Security

Two credentials, guarding two different things.

**`BROKER_SECRET` guards the API on port 8000.** Sent as `X-Broker-Secret`, compared in constant time. `GET /health` and `GET /verify` are deliberately exempt: `/health` so it works as a container healthcheck, `/verify` because nginx's `auth_request` cannot forward the secret and the stream token is itself the credential there.

**The stream token guards the desktop on 3000/3001, when you turn it on.** It is minted per session by `POST /launch`, 256 bits from `secrets.token_urlsafe`, and enforced by an nginx `auth_request` that the mod injects into *every* `server` block in the site config. That matters: the base image ships two identical vhosts, plain HTTP on 3000 and TLS on 3001, both proxying the same selkies stream and both serving `/config/Desktop` at `/files`. Gating only the TLS one left a complete bypass a port number away. The `stream_sid` cookie is `Secure`, so 3000 is usable only behind a TLS-terminating proxy: direct plain-HTTP browsing to it fails closed.

**The gate ships off, and that is a real hole.** With `STREAM_GATE=off`, anyone who can reach port 3000 or 3001 gets the interactive desktop with your ROM library browsable at `/files`, no credential asked for. It defaults to off because RomM cannot send the token yet. The streaming feature people are running is [rommapp/romm#3211](https://github.com/rommapp/romm/pull/3211), which is merged and hands the browser your configured `host` with nothing appended; the half that carries the token into the iframe URL is [rommapp/romm#3856](https://github.com/rommapp/romm/pull/3856), still open. Enforcing against a client that cannot comply is not a gate, it is a black stream: nginx refuses the document, every asset and the WebSocket upgrade alike, with nothing on screen to say why.

So: keep the container on a network you trust until #3856 ships, then set `STREAM_GATE=token` and restart. No recreate is needed, because enforcement is decided in the broker and the nginx gate is already injected either way. The broker logs which mode it is in at startup, and `GET /status` reports it as `stream_gate`.

**Leaving `BROKER_SECRET` unset is a known hole, not a supported mode.** The broker runs as root inside the container, so the open API means root-privileged reads and writes under `/config`, plus arbitrary launches within `ROM_ROOT`. Worse, the two credentials stop being independent: `POST /launch` *returns* a stream token, so an unauthenticated broker hands out the credential that opens the desktop. Use it for local debugging on a trusted host and nothing else. The broker logs a warning at startup when it is unset.

Port 8000 is plain HTTP, so the secret crosses the network in the clear. Keep the broker on an internal network, or put TLS in front of it.

## Troubleshooting

**Reading the logs.** Every request the broker refuses or fails is logged to stdout, not only returned as JSON, so `docker logs pcsx2` is enough to see what went wrong without turning on `DEBUG`. The shape is `HTTP <code> <method> <path> from <caller>: <reason>`. A `WARNING` is a caller-side rejection (bad slot, no game running, a claim already held); an `ERROR` is a broker-side fault (pactl failed, a state file that could not be written) and always deserves attention. An unhandled crash inside a handler logs a full traceback and still answers `500 internal broker error` rather than dropping the connection. Stream tokens are redacted from logged URLs, so log output is safe to paste into a bug report.

**The mod doesn't apply, or the broker never starts.** Check the image name (`ghcr.io/loneangelfayt/pcsx2-romm-integration-mod`), confirm the base image is `lscr.io/linuxserver/pcsx2:latest` or compatible, and run `docker compose up` without `-d` to watch the whole startup.

**Black screen for more than a minute.** 15-30 seconds is just the PS2 BIOS. Longer than that, check the BIOS files exist (`docker exec pcsx2 ls /config/bios`) and read PCSX2's own log (`docker exec pcsx2 tail -50 /config/.config/PCSX2/logs/emulog.txt`).

**Controllers dead.** Try `POST /cleanup` to restart Selkies and flush stale sockets. Confirm the sockets exist at all with `docker exec pcsx2 ls /tmp/selkies_js*.sock`, and check the broker logs for `LD_PRELOAD`. The mod pins the `selkies_gamepad` logger to `WARNING`, so the absence of per-socket `INFO:selkies_gamepad:` chatter is expected and not a sign the gamepads are gone; real gamepad warnings and errors still come through. Startup logs whether that patch applied, so `docker logs pcsx2 | grep broker-mod` tells you if it silently missed.

**Saves failing.** `docker logs pcsx2 | grep PINE`. No xdotool lines at all is healthy: xdotool only appears when PINE is unreachable. If you do see `PINE unavailable`, look for xdotool output, and remember the "found window" message is DEBUG-level while the "window not found" warning shows by default. Bump `PINE_WAIT` if saves are just slow, and check what actually landed with `docker exec pcsx2 ls /config/.config/PCSX2/sstates`.

**PCSX2 crashes right after a launch.** Read `PCSX2_LOG_PATH` for renderer and Vulkan errors, which is where a shader compile failure surfaces. Then check emulog for BIOS problems, and rule out a corrupt disc image.

**Nothing comes back after repeated crashes.** Check `/status`. `relaunch_abandoned: true` means PCSX2 exited within five seconds three times running and the broker deliberately stopped. Fix the underlying failure, then `POST /launch`, which clears the flag.

**A game "launches" but never boots.** An unbootable ROM doesn't crash PCSX2, it parks it on an error dialog, so the process stays alive, `/status` looks fine, and the crash-loop limiter never fires. Verify the image and the BIOS.

**RomM shows no play button.** Confirm `streaming.enabled: true`, confirm the platform slug matches, restart RomM after config changes, and check what RomM thinks it has: `curl http://romm:5000/api/streaming/config`.

**"A critical video error occurred. Resetting to default settings and reloading...", two or three times before the stream finally plays.** This is Selkies' client-side decoder fallback, not the broker. The browser's WebCodecs `VideoDecoder` throws a fatal error on the H.264 stream, so the client closes the WebSocket, downgrades its encoder setting, and reloads after three seconds. On the third failure it switches to `jpeg`, which forces CPU encoding and works, which is why it always plays *eventually*. It never self-heals, because the client zeroes its crash counter when it lands on `jpeg` and the next launch starts over from the server default.

The usual cause is the hardware encoder: Selkies routes `x264enc` through VAAPI whenever a render node is present, and the container log shows `[Wayland] Initializing Unified VAAPI Encoder...` right before each crash. Force software encoding to keep H.264 without the VAAPI path:

```yaml
- SELKIES_USE_CPU=true
```

Confirm it took with `docker logs pcsx2 | grep -E "VAAPI|CPU Software"`. If it still cycles, drop to `SELKIES_ENCODER=jpeg` to skip H.264 entirely, at a real cost in bandwidth and sharpness.

**"An error occurred, restarting stream", over and over.** The stream client says this whenever its WebSocket drops. First establish which mode is live: the startup line in `docker logs pcsx2` names it, and so does `stream_gate` in `GET /status`. Under `STREAM_GATE=off`, `/verify` admits every request with a `200`, and only 4xx and 5xx responses are logged, so no `/verify` lines at all is the expected healthy state there, not a fault. Under `STREAM_GATE=token`, the useful signal is on the broker side: `docker logs pcsx2 | grep "/verify"`. A `403` there names the reason (`no stream token in the request`, `stream token expired`, `stream token superseded by a newer launch`), and the token is redacted so the line is safe to share. No `/verify` lines at all while in `token` mode does mean the gate is not running: the mod's init never patched nginx. Check `docker logs pcsx2 | grep broker-mod` for the "Applied nginx stream gate to N vhost(s)" line, which should say 2; that check is valid in either mode, since the gate config is injected regardless of `STREAM_GATE`.

**A stream that 403s on every asset over plain HTTP.** Only possible under `STREAM_GATE=token`, see [Security](#security): under `off` nothing 403s. The `stream_sid` cookie is `Secure`, so a browser will not store it on an unencrypted origin. The query token admits the first request and everything after it is refused. Reach the container over TLS: either port 3001 directly, or port 3000 behind a reverse proxy that terminates TLS. Plain-HTTP 3000 straight to a browser is not a supported shape in `token` mode.

## Development

The broker is one stdlib Python file with a stdlib `unittest` suite. Nothing to install, which is the point: the container has no pip, so the broker cannot grow a dependency without breaking the image.

```bash
python3 -m unittest discover -s tests -v
```

CI runs the same tests under pytest, plus `ruff check .` against the shared [ruff.toml](ruff.toml). `tests/` is excluded from the image by `.dockerignore` and never ships.

The suite covers what can be tested without a running emulator: save-file and memory-card archive handling (round trips, the last-write-wins mtime guard, path-traversal and subtree rejection, File-card refusal), `PCSX2.ini` patching, ROM path resolution, the session state machine (crash-loop limiter, launch and card-op claims, deferred `load_slot` generation checks), the stream-token gate decision, and the header/secret handling that keeps a filename off the response line and a UTF-8 secret out of a 500. Anything needing a real X display, PINE socket, or pcsx2-qt process is out of scope on purpose, because it is only provable on a live container.

**Init ordering matters more than it looks.** The mod ships two s6 oneshots. `init-pcsx2-config` rewrites files that base-image services read exactly once at startup (selkies' `input_handler.py`, the nginx site config, labwc's autostart), so `init-services` is made to depend on it and the whole service stack waits. Drop that edge and the patches still land on disk while changing nothing about the processes already running, which fails silently: the log says "Applied nginx stream gate" and the live nginx has no gate. Keep that script fast and offline. `init-pcsx2-deps` holds the slow networked work (apt, `patches.zip`) and only `svc-broker` waits for it, so a GitHub timeout cannot stall the stream.

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) and releases are cut automatically on merge to `main`: `fix:` bumps the patch, `feat:` the minor, `feat!:` the major.

## Pinning a version

```yaml
- DOCKER_MODS=ghcr.io/loneangelfayt/pcsx2-romm-integration-mod:v1.3.1   # exact
- DOCKER_MODS=ghcr.io/loneangelfayt/pcsx2-romm-integration-mod:v1.3     # patches only
- DOCKER_MODS=ghcr.io/loneangelfayt/pcsx2-romm-integration-mod:latest   # always newest
```

## Resources

- [Releases and changelog](https://github.com/LoneAngelFayt/pcsx2-romm-integration/releases) and the [published images](https://github.com/LoneAngelFayt/pcsx2-romm-integration/pkgs/container/pcsx2-romm-integration-mod)
- [RomM](https://github.com/rommapp/romm) and its [wiki](https://github.com/rommapp/romm/wiki)
- [linuxserver/pcsx2](https://docs.linuxserver.io/images/docker-pcsx2/) and [how Docker Mods work](https://docs.linuxserver.io/general/container-customization)
- [PCSX2 configuration guide](https://pcsx2.net/docs/) and [PINE](https://github.com/PCSX2/pcsx2/blob/master/pcsx2/PINE.h), the IPC protocol behind save states
- [Selkies](https://github.com/selkies-project/selkies), which does the WebRTC streaming
- Sibling brokers, same shape, different emulators: [Dolphin](https://github.com/LoneAngelFayt/dolphin-romm-integration), [xemu](https://github.com/LoneAngelFayt/xemu-romm-integration), [RPCS3](https://github.com/LoneAngelFayt/rpcs3-romm-integration), [Eden](https://github.com/LoneAngelFayt/eden-romm-integration)

## License

[GPLv3](LICENSE)
