#!/usr/bin/env python3
# ROM launch broker for the linuxserver/pcsx2 Docker mod.
# Accepts HTTP requests from RomM to launch/stop games and save state on release.

import json
import logging
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread, Lock

# ── Config ────────────────────────────────────────────────────────────────────

PORT               = int(os.environ.get("BROKER_PORT", "8000"))
DISPLAY            = os.environ.get("DISPLAY", ":1")
SECRET             = os.environ.get("BROKER_SECRET", "")
HEARTBEAT_TIMEOUT  = int(os.environ.get("BROKER_HEARTBEAT_TIMEOUT", "120"))  # seconds; 0 = disabled

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [broker] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("broker")

# ── Session state ─────────────────────────────────────────────────────────────

_session: dict = {}
_heartbeat_lock = Lock()
_last_heartbeat: float = 0.0  # monotonic timestamp of last heartbeat; 0 = no active session

# ── Process helpers ───────────────────────────────────────────────────────────

ROM_FILE       = "/tmp/pcsx2-rom"
SSTATES_DIR    = "/config/.config/PCSX2/sstates"
PINE_SOCK      = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/config/.XDG"), "pcsx2.sock")
PINE_SAVE_SLOT = int(os.environ.get("BROKER_SAVE_SLOT", "0"))

# xdotool and pactl must run as abc — xwayland and pulseaudio are scoped to that user's session.
_ABC_UID = 1000
_ABC_GID = 1000
_ABC_ENV = {
    "DISPLAY": ":0",
    "HOME": "/config",
    "USER": "abc",
    "XDG_RUNTIME_DIR": "/config/.XDG",
    "WAYLAND_DISPLAY": "wayland-1",
}

def _as_abc():
    """preexec_fn: drop to abc before execing a subprocess."""
    os.setgid(_ABC_GID)
    os.setuid(_ABC_UID)

# PINE opcodes for PCSX2-Qt
_PINE_SAVE_STATE = 9
_PINE_LOAD_STATE = 10


def _pine_save_state(slot: int = PINE_SAVE_SLOT) -> bool:
    """Trigger a save state via PINE IPC and wait for the .p2s file to be written."""
    if not Path(PINE_SOCK).exists():
        log.info("PINE socket not found — no game running, skipping save state")
        return False
    try:
        # Record mtimes before saving so we can detect when the file is written
        before = {p: p.stat().st_mtime for p in Path(SSTATES_DIR).glob("*.p2s")} if Path(SSTATES_DIR).exists() else {}

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5.0)
            s.connect(PINE_SOCK)
            # [u32 total_len][u8 opcode][u8 slot] — 6 bytes
            s.sendall(struct.pack("<IBB", 6, _PINE_SAVE_STATE, slot))
            resp = s.recv(5)  # [u32 total_len][u8 result]
            if len(resp) >= 5:
                result = struct.unpack("<IB", resp[:5])[1]
                if result != 0:
                    log.warning("PINE save state returned error code %d", result)
                    return False

        log.info("Save state queued for slot %d", slot)

        # Poll up to 5s for the .p2s file to appear or update
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            time.sleep(0.25)
            for p in Path(SSTATES_DIR).glob("*.p2s"):
                if p not in before or p.stat().st_mtime > before.get(p, 0):
                    log.info("Save state written: %s", p)
                    return True

        log.warning("Save state file did not appear within timeout")
        return True  # IPC command accepted; write may still be in progress

    except Exception as exc:
        log.warning("PINE save state failed: %s", exc)
    return False


def _pine_load_state(slot: int = PINE_SAVE_SLOT) -> bool:
    """Trigger a load state via PINE IPC."""
    if not Path(PINE_SOCK).exists():
        log.info("PINE socket not found — no game running, skipping load state")
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5.0)
            s.connect(PINE_SOCK)
            # [u32 total_len][u8 opcode][u8 slot] — 6 bytes
            s.sendall(struct.pack("<IBB", 6, _PINE_LOAD_STATE, slot))
            resp = s.recv(5)  # [u32 total_len][u8 result]
            if len(resp) >= 5:
                result = struct.unpack("<IB", resp[:5])[1]
                if result != 0:
                    log.warning("PINE load state returned error code %d", result)
                    return False
        log.info("Load state queued for slot %d", slot)
        return True
    except Exception as exc:
        log.warning("PINE load state failed: %s", exc)
    return False


def _take_screenshot() -> bool:
    """Send F8 to PCSX2's X11 window to trigger a screenshot."""
    try:
        result = subprocess.run(
            ["xdotool", "search", "--classname", "pcsx2-qt", "key", "--clearmodifiers", "F8"],
            capture_output=True,
            env=_ABC_ENV,
            preexec_fn=_as_abc,
        )
        if result.returncode == 0:
            log.info("Screenshot triggered via xdotool")
            return True
        log.warning("xdotool screenshot failed: %s", result.stderr.decode().strip())
    except Exception as exc:
        log.warning("Screenshot failed: %s", exc)
    return False


def _set_volume(level: int) -> bool:
    """Set PCSX2's PulseAudio sink input volume. level is 0–100."""
    level = max(0, min(100, level))
    try:
        # Find the sink input index owned by the pcsx2-qt process
        result = subprocess.run(["pactl", "list", "sink-inputs"], capture_output=True, text=True,
                                env=_ABC_ENV, preexec_fn=_as_abc)
        sink_input = None
        current_index = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("Sink Input #"):
                current_index = line.split("#")[1]
            elif "application.process.binary" in line and "pcsx2-qt" in line:
                sink_input = current_index
                break

        if sink_input is None:
            log.warning("Could not find PCSX2 sink input — is audio running?")
            return False

        subprocess.run(
            ["pactl", "set-sink-input-volume", sink_input, f"{level}%"],
            capture_output=True, check=True,
            env=_ABC_ENV, preexec_fn=_as_abc,
        )
        log.info("Volume set to %d%% (sink input %s)", level, sink_input)
        return True
    except Exception as exc:
        log.warning("Volume set failed: %s", exc)
    return False


def _kill_pcsx2() -> None:
    """SIGTERM with 8s grace period, SIGKILL if it doesn't exit.
    Captures target PIDs upfront to avoid race conditions with newly launched processes."""
    try:
        # Get all current pcsx2-qt PIDs
        res = subprocess.run(["pgrep", "-f", "pcsx2-qt"], capture_output=True, text=True)
        pids = [p.strip() for p in res.stdout.splitlines() if p.strip()]
        
        if not pids:
            return

        log.info("Requesting graceful stop (SIGTERM) for PCSX2 (PIDs: %s)...", ", ".join(pids))
        for pid in pids:
            subprocess.run(["kill", "-15", pid], capture_output=True)

        # Wait up to 8s for these specific PIDs to disappear
        for _ in range(16):
            remaining = []
            for pid in pids:
                if subprocess.run(["kill", "-0", pid], capture_output=True).returncode == 0:
                    remaining.append(pid)
            
            if not remaining:
                log.info("PCSX2-Qt exited gracefully.")
                return
            time.sleep(0.5)

        # Force SIGKILL on any that are still around
        res = subprocess.run(["pgrep", "-f", "pcsx2-qt"], capture_output=True, text=True)
        current_pids = [p.strip() for p in res.stdout.splitlines() if p.strip()]
        for pid in pids:
            if pid in current_pids:
                log.warning("PCSX2-Qt (PID %s) didn't close in time. Forcing SIGKILL...", pid)
                subprocess.run(["kill", "-9", pid], capture_output=True)
                
    except Exception as exc:
        log.warning("Error during PCSX2 shutdown: %s", exc)


# ── Heartbeat monitor ────────────────────────────────────────────────────────

def _release_session():
    """Save state and return to dashboard. Shared by DELETE /launch and heartbeat timeout."""
    global _last_heartbeat
    if not _session:
        return

    _pine_save_state()
    Path(ROM_FILE).unlink(missing_ok=True)
    _kill_pcsx2()
    _session.clear()
    with _heartbeat_lock:
        _last_heartbeat = 0.0


def _heartbeat_monitor():
    """Background thread: auto-release if no heartbeat received within HEARTBEAT_TIMEOUT seconds."""
    while True:
        time.sleep(10)
        if HEARTBEAT_TIMEOUT <= 0 or not _session:
            continue
        with _heartbeat_lock:
            last = _last_heartbeat
        if last > 0 and time.monotonic() - last > HEARTBEAT_TIMEOUT:
            log.warning("Heartbeat timeout (%ds) — auto-releasing session", HEARTBEAT_TIMEOUT)
            _release_session()
            log.info("Auto-release complete")


# ── HTTP handler ──────────────────────────────────────────────────────────────

class BrokerHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.debug("HTTP %s", fmt % args)

    def _check_secret(self) -> bool:
        if not SECRET:
            return True
        return self.headers.get("X-Broker-Secret", "") == SECRET

    def _send_json(self, code: int, body: dict) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif parsed.path == "/status":
            if _session:
                self._send_json(200, {
                    "active": True,
                    "rom_path": _session.get("rom_path"),
                    "rom_name": _session.get("rom_name"),
                    "started_at": _session.get("started_at"),
                    "paused": _session.get("paused", False),
                })
            else:
                self._send_json(200, {"active": False})
        elif parsed.path == "/savefile":
            if not self._check_secret():
                self._send_json(403, {"error": "forbidden"})
                return
            card = int(params.get("card", ["1"])[0])
            if card not in (1, 2):
                self._send_json(400, {"error": "card must be 1 or 2"})
                return
            memcard = Path(f"/config/.config/PCSX2/memcards/Mcd00{card}.ps2")
            if not memcard.exists():
                self._send_json(404, {"error": f"memcard {card} not found"})
                return
            data = memcard.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="Mcd00{card}.ps2"')
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)
            log.info("Exported memcard %d (%d bytes)", card, len(data))
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/heartbeat":
            if not self._check_secret():
                self._send_json(403, {"error": "forbidden"})
                return
            if _session:
                global _last_heartbeat
                with _heartbeat_lock:
                    _last_heartbeat = time.monotonic()
                self._send_json(200, {"status": "ok"})
            else:
                self._send_json(200, {"status": "no_session"})
            return

        if self.path == "/savestate":
            if not self._check_secret():
                self._send_json(403, {"error": "forbidden"})
                return
            if not _session:
                self._send_json(409, {"error": "no active session"})
                return
            body = self._read_body()
            slot = int(body.get("slot", PINE_SAVE_SLOT))
            def _do_save(slot=slot):
                ok = _pine_save_state(slot)
                log.info("Manual save state slot %d %s", slot, "succeeded" if ok else "failed")
            Thread(target=_do_save, daemon=True).start()
            self._send_json(200, {"status": "saving", "slot": slot})
            return

        if self.path == "/pause":
            if not self._check_secret():
                self._send_json(403, {"error": "forbidden"})
                return
            if not _session:
                self._send_json(409, {"error": "no active session"})
                return
            paused = _session.get("paused", False)
            sig = "SIGCONT" if paused else "SIGSTOP"
            result = subprocess.run(["pkill", f"-{sig}", "-f", "pcsx2-qt"], capture_output=True)
            if result.returncode == 0:
                _session["paused"] = not paused
                state = "resumed" if paused else "paused"
                log.info("PCSX2 %s", state)
                self._send_json(200, {"status": state, "paused": not paused})
            else:
                self._send_json(502, {"error": "no PCSX2 process found"})
            return

        if self.path == "/restart":
            if not self._check_secret():
                self._send_json(403, {"error": "forbidden"})
                return
            if not _session:
                self._send_json(409, {"error": "no active session"})
                return
            rom_path = _session.get("rom_path")
            def _do_restart(rom_path=rom_path):
                log.info("Restart: killing PCSX2 and relaunching %s", rom_path)
                _kill_pcsx2()
                Path(ROM_FILE).write_text(rom_path)
                log.info("Restart: launcher signal written")
            Thread(target=_do_restart, daemon=True).start()
            self._send_json(200, {"status": "restarting", "rom_path": rom_path})
            return

        if self.path == "/volume":
            if not self._check_secret():
                self._send_json(403, {"error": "forbidden"})
                return
            body = self._read_body()
            if "level" not in body:
                self._send_json(400, {"error": "level is required (0–100)"})
                return
            level = int(body["level"])
            ok = _set_volume(level)
            self._send_json(200 if ok else 502, {"status": "ok" if ok else "failed", "level": level})
            return

        if self.path == "/screenshot":
            if not self._check_secret():
                self._send_json(403, {"error": "forbidden"})
                return
            if not _session:
                self._send_json(409, {"error": "no active session"})
                return
            ok = _take_screenshot()
            self._send_json(200 if ok else 502, {"status": "ok" if ok else "failed"})
            return

        if self.path == "/loadstate":
            if not self._check_secret():
                self._send_json(403, {"error": "forbidden"})
                return
            if not _session:
                self._send_json(409, {"error": "no active session"})
                return
            body = self._read_body()
            slot = int(body.get("slot", PINE_SAVE_SLOT))
            def _do_load(slot=slot):
                ok = _pine_load_state(slot)
                log.info("Manual load state slot %d %s", slot, "succeeded" if ok else "failed")
            Thread(target=_do_load, daemon=True).start()
            self._send_json(200, {"status": "loading", "slot": slot})
            return

        if self.path != "/launch":
            self._send_json(404, {"error": "not found"})
            return
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return

        body = self._read_body()
        rom_path = body.get("rom_path", "").strip()
        rom_name = body.get("rom_name", Path(rom_path).stem if rom_path else "")

        if not rom_path:
            self._send_json(400, {"error": "rom_path is required"})
            return

        if not Path(rom_path).exists():
            log.warning("ROM not found: %s", rom_path)
            self._send_json(422, {
                "error": "rom_path does not exist inside the container",
                "rom_path": rom_path,
                "hint": "Check that your ROMs volume is mounted at the same path in both containers",
            })
            return

        def _do_launch():
            global _last_heartbeat
            # write rom file before killing -- launcher sees it the moment pcsx2 exits.
            # safe because _kill_pcsx2() captures pids upfront and won't kill the new process.
            Path(ROM_FILE).write_text(rom_path)
            try:
                os.chown(ROM_FILE, 1000, 1000)
            except OSError:
                pass
            log.info("Wrote ROM path to launcher signal file: %s", rom_path)
            _kill_pcsx2()
            _session.update({
                "rom_path": rom_path,
                "rom_name": rom_name,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            with _heartbeat_lock:
                _last_heartbeat = time.monotonic()
            log.info("Session started: %s", rom_name)

        Thread(target=_do_launch, daemon=True).start()
        self._send_json(200, {
            "status": "launched",
            "rom_path": rom_path,
            "rom_name": rom_name,
        })

    def do_DELETE(self):
        if self.path != "/launch":
            self._send_json(404, {"error": "not found"})
            return
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return

        def _do_soft_reset():
            log.info("Release: saving state then stopping game")
            _release_session()
            log.info("Release complete — returned to dashboard")

        Thread(target=_do_soft_reset, daemon=True).start()
        self._send_json(200, {"status": "stopping"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Broker-Secret")
        self.end_headers()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    Thread(target=_heartbeat_monitor, daemon=True).start()

    server = HTTPServer(("0.0.0.0", PORT), BrokerHandler)
    log.info("ROM broker listening on port %d", PORT)
    log.info("DISPLAY=%s", DISPLAY)
    if HEARTBEAT_TIMEOUT > 0:
        log.info("Heartbeat timeout: %ds", HEARTBEAT_TIMEOUT)
    else:
        log.info("Heartbeat timeout disabled")
    if SECRET:
        log.info("Shared secret auth enabled")
    else:
        log.warning("No BROKER_SECRET set — unauthenticated access allowed")

    bios_dir = Path("/config/bios")
    if not bios_dir.exists() or not any(bios_dir.iterdir()):
        log.warning("No BIOS files found at /config/bios — PCSX2 will not run games until a PS2 BIOS is mounted there")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
