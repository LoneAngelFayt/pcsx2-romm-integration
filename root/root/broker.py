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


def _kill_pcsx2() -> None:
    """SIGTERM with 8s grace period, SIGKILL if it doesn't exit."""
    try:
        if subprocess.run(["pgrep", "-f", "pcsx2-qt"], capture_output=True).returncode == 0:
            log.info("Requesting graceful stop (SIGTERM) for PCSX2...")
            subprocess.run(["pkill", "-15", "-f", "pcsx2-qt"], capture_output=True)

            for _ in range(16):
                if subprocess.run(["pgrep", "-f", "pcsx2-qt"], capture_output=True).returncode != 0:
                    log.info("PCSX2-Qt exited gracefully.")
                    return
                time.sleep(0.5)

            log.warning("PCSX2-Qt didn't close in time. Forcing exit...")
            subprocess.run(["pkill", "-9", "-f", "pcsx2-qt"], capture_output=True)
    except Exception as exc:
        log.warning("Error during PCSX2 shutdown: %s", exc)


# ── Heartbeat monitor ────────────────────────────────────────────────────────

def _release_session():
    """Save state and return to dashboard. Shared by DELETE /launch and heartbeat timeout."""
    global _last_heartbeat
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
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif self.path == "/status":
            if _session:
                self._send_json(200, {
                    "active": True,
                    "rom_path": _session.get("rom_path"),
                    "rom_name": _session.get("rom_name"),
                    "started_at": _session.get("started_at"),
                })
            else:
                self._send_json(200, {"active": False})
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
            Path(ROM_FILE).write_text(rom_path)
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
