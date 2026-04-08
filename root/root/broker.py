#!/usr/bin/env python3
"""broker.py — launch PCSX2 on demand and expose a small HTTP API."""

import glob
import hmac
import json
import logging
import os
import signal
import socket as _socket
import struct
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread, Lock

# ── Config ────────────────────────────────────────────────────────────────────

PORT     = int(os.environ.get("BROKER_PORT", "8000"))
SECRET   = os.environ.get("BROKER_SECRET", "")
ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm/library")).resolve()

ENV = {
    "DISPLAY":           ":0",
    "WAYLAND_DISPLAY":   "wayland-1",
    "XDG_RUNTIME_DIR":   "/config/.XDG",
    "PULSE_RUNTIME_PATH":"/defaults",
    "LD_PRELOAD":        "/usr/lib/selkies_joystick_interposer.so",
    "HOME":              "/config",
    "USER":              "abc",
    "QT_QPA_PLATFORM":   "xcb",
}

INI_PATH = Path("/config/.config/PCSX2/inis/PCSX2.ini")

PINE_SLOT    = int(os.environ.get("PINE_SLOT", "28011"))
PINE_SOCKET  = Path(ENV["XDG_RUNTIME_DIR"]) / f"pcsx2-{PINE_SLOT}"
PINE_TIMEOUT = float(os.environ.get("PINE_TIMEOUT", "5.0"))
SAVE_SLOT    = int(os.environ.get("SAVE_SLOT", "0"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [broker] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("broker")

# ── Session state ─────────────────────────────────────────────────────────────

_session_lock = Lock()
_session: dict = {
    "process":          None,
    "rom_path":         None,
    "rom_name":         None,
    "started_at":       None,
    "is_managed":       False,
    "save_in_progress": False,
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_rom_path(raw: str) -> Path | None:
    """Resolve raw to an absolute path and confirm it lives under ROM_ROOT."""
    try:
        p = Path(raw).resolve()
    except (ValueError, OSError):
        return None
    if not p.is_relative_to(ROM_ROOT):
        return None
    return p


def _patch_ini():
    if not INI_PATH.exists():
        log.warning("PCSX2.ini not found at %s — skipping patch", INI_PATH)
        return
    try:
        patches = {
            "EnablePINE":      "EnablePINE = true",
            "StartFullscreen": "StartFullscreen = true",
            "ConfirmShutdown": "ConfirmShutdown = false",
        }
        lines = INI_PATH.read_text().splitlines()
        applied = set()
        new_lines = []
        for line in lines:
            matched = False
            for key, val in patches.items():
                if line.strip().startswith(f"{key} =") or line.strip().startswith(f"{key}="):
                    new_lines.append(val)
                    applied.add(key)
                    matched = True
                    break
            if not matched:
                new_lines.append(line)
        for key, val in patches.items():
            if key not in applied:
                log.warning("PCSX2.ini: %s not found — appending without section header", key)
                new_lines.append(val)
        INI_PATH.write_text("\n".join(new_lines) + "\n")
        log.info("PCSX2.ini patched (PINE, Fullscreen, NoConfirmShutdown)")
    except Exception as exc:
        log.error("Failed to patch PCSX2.ini: %s", exc)


def _kill_pcsx2():
    """Kill the managed pcsx2-qt process group. Lock is released before waiting."""
    with _session_lock:
        _session["is_managed"] = False
        proc = _session["process"]
        _session["process"] = None

    if proc is None or proc.poll() is not None:
        return

    log.info("Stopping PCSX2 (PID %d)...", proc.pid)
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("PCSX2 did not exit after SIGTERM — sending SIGKILL")
            os.killpg(pgid, signal.SIGKILL)
            proc.wait()
    except ProcessLookupError:
        pass  # already gone


def _launch_pcsx2_internal(rom_path):
    """Launch pcsx2-qt as abc via sudo+env. Inline env vars bypass sudo's env scrubbing."""
    cmd = [
        "sudo", "-u", "abc", "env",
        *[f"{k}={v}" for k, v in ENV.items()],
        "pcsx2-qt",
    ]
    if rom_path:
        # '--' terminates option parsing so a path that starts with '-' isn't
        # treated as a pcsx2-qt flag.
        cmd.extend(["-batch", "-fullscreen", "--", rom_path])

    log.info("Launching: %s", " ".join(cmd))

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setpgrp,  # own process group so killpg is clean
        )
    except Exception as exc:
        log.error("Failed to launch PCSX2: %s", exc)
        with _session_lock:
            _session["process"] = None
            _session["is_managed"] = False
        return

    with _session_lock:
        _session["process"] = proc
        _session["is_managed"] = True
    log.info("PCSX2 launched (PID %d)", proc.pid)
    Thread(target=_monitor_process, args=(proc, time.monotonic()), daemon=True).start()


def _monitor_process(proc, start_time):
    """On unexpected exit, relaunch into dashboard mode if the session is still managed."""
    proc.wait()
    duration = time.monotonic() - start_time

    with _session_lock:
        should_relaunch = _session["is_managed"] and _session["process"] is proc

    if not should_relaunch:
        return

    # Back off if the process died almost immediately to avoid a tight crash loop.
    wait_time = 5 if duration < 5 else 1
    log.info("PCSX2 exited after %.1fs — relaunching dashboard in %ds", duration, wait_time)
    time.sleep(wait_time)

    with _session_lock:
        # Re-check: _kill_pcsx2 may have fired during the sleep above.
        if not _session["is_managed"]:
            return
        _session["rom_path"] = None
        _session["rom_name"] = "Dashboard"
        _session["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    _launch_pcsx2_internal(None)


def _drain_gamepad_sockets():
    """Send EOF to each selkies gamepad socket before launching a new session.

    The selkies input_handler has two phases per connection:
      1. Sends config payload, awaits a 1-byte arch specifier from the client.
      2. Keep-alive loop: while self.running and not writer.is_closing().

    Connecting and immediately sending SHUT_WR causes readexactly(1) in phase 1
    to raise IncompleteReadError — the handler exits and removes itself from the
    active client list without ever entering phase 2.

    Phase-2 handlers are unaffected; their loop has no EOF check. They clear on
    selkies restart or once the reader.at_eof() patch is active

    Socket files that refuse connection are stale and are unlinked.
    """
    paths = sorted(
        glob.glob("/tmp/selkies_js*.sock") + glob.glob("/tmp/selkies_event*.sock")
    )
    if not paths:
        log.info("Socket drain: no gamepad sockets found.")
        return

    drained = 0
    removed = 0
    for path in paths:
        try:
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                s.connect(path)
                s.shutdown(_socket.SHUT_WR)
            drained += 1
        except OSError:
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass

    log.info(
        "Socket drain: sent EOF to %d socket(s), removed %d dead file(s) (of %d total).",
        drained, removed, len(paths),
    )


def _launch_pcsx2(rom_path):
    _kill_pcsx2()
    _drain_gamepad_sockets()
    _patch_ini()
    time.sleep(2)  # let drained handlers exit and the kill settle before launching
    with _session_lock:
        _session["rom_path"] = rom_path
        _session["rom_name"] = Path(rom_path).stem if rom_path else "Dashboard"
        _session["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _launch_pcsx2_internal(rom_path)


def _recv_exact(s: _socket.socket, n: int) -> bytes:
    """Read exactly n bytes from a socket, accumulating across partial reads."""
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"PINE: socket closed after {len(buf)}/{n} bytes")
        buf.extend(chunk)
    return bytes(buf)


def _find_pine_socket() -> Path | None:
    """Locate PCSX2's PINE Unix socket. Tries the configured path first, then
    falls back to a glob search so the correct name is discovered automatically."""
    if PINE_SOCKET.exists():
        return PINE_SOCKET
    runtime_dir = Path(ENV["XDG_RUNTIME_DIR"])
    for pattern in ("pcsx2-*", "pcsx2.sock"):
        found = sorted(runtime_dir.glob(pattern))
        if found:
            log.info("PINE: configured path %s absent — using %s", PINE_SOCKET, found[0])
            return found[0]
    for pattern in ("pcsx2-*", "pcsx2.sock"):
        found = sorted(Path("/tmp").glob(pattern))
        if found:
            log.info("PINE: found socket in /tmp at %s", found[0])
            return found[0]
    log.error("PINE: no socket found (looked in %s and /tmp)", runtime_dir)
    return None


def _pine_save_state(slot: int) -> bool:
    """Send MsgSaveState (opcode 9) to PCSX2 via the PINE Unix socket.

    Wire format: [uint32 LE: payload length] [0x09] [slot byte]
    Response:    [uint32 LE: 0] (empty body — save is fire-and-ack)
    Returns True if PCSX2 acknowledged the command.
    """
    socket_path = _find_pine_socket()
    if socket_path is None:
        return False
    payload = bytes([9, slot & 0xFF])
    msg = struct.pack("<I", len(payload)) + payload
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(PINE_TIMEOUT)
            s.connect(str(socket_path))
            s.sendall(msg)
            resp_len = struct.unpack("<I", _recv_exact(s, 4))[0]
            if resp_len > 0:
                _recv_exact(s, resp_len)
        log.info("PINE: save state to slot %d OK", slot)
        return True
    except Exception as exc:
        log.error("PINE save state (slot %d) failed: %s", slot, exc)
        return False


def _save_and_exit(slot: int) -> bool:
    """Save emulator state then kill PCSX2. Returns True if save succeeded."""
    ok = _pine_save_state(slot)
    _kill_pcsx2()
    return ok


def _cleanup_sockets():
    """Restart selkies to flush all stale gamepad connections.
    s6-overlay brings it back automatically within a few seconds."""
    log.info("Socket cleanup: restarting selkies...")
    result = subprocess.run(["pkill", "-15", "-f", "selkies"], capture_output=True)
    if result.returncode == 0:
        log.info("Socket cleanup: selkies stopped, s6 will restart it shortly.")
    else:
        log.warning("Socket cleanup: selkies not found or already stopped.")


# ── HTTP handler ──────────────────────────────────────────────────────────────

class BrokerHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.debug("HTTP %s", fmt % args)

    def _check_secret(self) -> bool:
        if not SECRET:
            return True
        return hmac.compare_digest(
            self.headers.get("X-Broker-Secret", ""),
            SECRET,
        )

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
            with _session_lock:
                active = (
                    _session["process"] is not None
                    and _session["process"].poll() is None
                )
                snap = dict(_session) if active else {}
            self._send_json(200, {
                "active":     active,
                "rom_path":   snap.get("rom_path")   if active else None,
                "rom_name":   snap.get("rom_name")   if active else None,
                "started_at": snap.get("started_at") if active else None,
            })
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return

        if self.path == "/cleanup":
            Thread(target=_cleanup_sockets, daemon=True).start()
            self._send_json(200, {"status": "cleanup started"})
            return

        if self.path == "/save-and-exit":
            with _session_lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _session["save_in_progress"]:
                    self._send_json(409, {"error": "save already in progress"})
                    return
                _session["save_in_progress"] = True
            body = self._read_body()
            slot = body.get("slot", SAVE_SLOT)
            if not isinstance(slot, int) or not (0 <= slot <= 9):
                with _session_lock:
                    _session["save_in_progress"] = False
                self._send_json(400, {"error": "slot must be 0–9"})
                return
            wait = body.get("wait", True)
            if wait:
                try:
                    ok = _save_and_exit(slot)
                finally:
                    with _session_lock:
                        _session["save_in_progress"] = False
                self._send_json(200, {"status": "ok", "saved": ok, "slot": slot})
                # Relaunch to dashboard after save — same path as DELETE /launch.
                Thread(target=_launch_pcsx2, args=(None,), daemon=True).start()
            else:
                def _bg(s):
                    try:
                        _save_and_exit(s)
                    finally:
                        with _session_lock:
                            _session["save_in_progress"] = False
                    # Relaunch to dashboard after save — same path as DELETE /launch.
                    _launch_pcsx2(None)
                Thread(target=_bg, args=(slot,), daemon=True).start()
                # Session state is not yet cleared when this response is sent;
                # callers polling /status immediately may observe stale state.
                self._send_json(200, {"status": "queued", "slot": slot})
            return

        if self.path != "/launch":
            self._send_json(404, {"error": "not found"})
            return

        with _session_lock:
            if _session["save_in_progress"]:
                self._send_json(409, {"error": "save in progress"})
                return

        body = self._read_body()
        raw_path = body.get("rom_path", "").strip()

        if not raw_path:
            self._send_json(400, {"error": "rom_path is required"})
            return

        rom_path = _validate_rom_path(raw_path)
        if rom_path is None:
            self._send_json(400, {
                "error": "rom_path must be within ROM_ROOT",
                "rom_root": str(ROM_ROOT),
            })
            return
        if not rom_path.exists():
            self._send_json(422, {"error": "rom_path does not exist", "path": str(rom_path)})
            return

        Thread(target=_launch_pcsx2, args=(str(rom_path),), daemon=True).start()
        self._send_json(200, {"status": "launching", "rom_path": str(rom_path)})

    def do_DELETE(self):
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return
        if self.path != "/launch":
            self._send_json(404, {"error": "not found"})
            return

        Thread(target=_launch_pcsx2, args=(None,), daemon=True).start()
        log.info("Soft reset: returning to dashboard")
        self._send_json(200, {"status": "resetting"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Broker-Secret")
        self.end_headers()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("Broker starting — waiting 5s for desktop...")
    time.sleep(5)

    # Safety net for stale processes on hot-reload; init-pcsx2-config already
    # disables the labwc autostart so this should normally find nothing.
    result = subprocess.run(["pkill", "-9", "-f", "pcsx2-qt"], capture_output=True)
    if result.returncode == 0:
        log.info("Killed stale pcsx2-qt instance(s) on startup.")
        time.sleep(2)

    _patch_ini()
    _launch_pcsx2_internal(None)

    server = HTTPServer(("0.0.0.0", PORT), BrokerHandler)
    log.info("ROM broker listening on port %d", PORT)
    if SECRET:
        log.info("Shared secret auth enabled")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
