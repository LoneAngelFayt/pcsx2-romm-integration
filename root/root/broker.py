#!/usr/bin/env python3
"""
broker.py — ROM launch broker for linuxserver/pcsx2 container
Directly launches pcsx2-qt as 'abc' user.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread, Lock

# ── Config ────────────────────────────────────────────────────────────────────

PORT    = int(os.environ.get("BROKER_PORT", "8000"))
SECRET  = os.environ.get("BROKER_SECRET", "")

# Environment for pcsx2-qt
ENV = {
    "DISPLAY": ":0",
    "WAYLAND_DISPLAY": "wayland-1",
    "XDG_RUNTIME_DIR": "/config/.XDG",
    "PULSE_RUNTIME_PATH": "/defaults",
    "HOME": "/config",
    "USER": "abc",
    "QT_QPA_PLATFORM": "xcb",
}

INI_PATH = Path("/config/.config/PCSX2/inis/PCSX2.ini")

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
    "process": None,
    "rom_path": None,
    "rom_name": None,
    "started_at": None,
    "is_managed": False, # If true, we want to keep it running
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _patch_ini():
    """Ensure PCSX2 is configured for headless/remote launch."""
    if not INI_PATH.exists():
        log.warning("PCSX2.ini not found at %s. Skipping patch.", INI_PATH)
        return

    try:
        content = INI_PATH.read_text()
        lines = content.splitlines()
        new_lines = []
        
        # Simple line-by-line replacement for key settings
        patches = {
            "EnablePINE": "EnablePINE = true",
            "StartFullscreen": "StartFullscreen = true",
            "SetupWizardIncomplete": "SetupWizardIncomplete = false",
            "ConfirmShutdown": "ConfirmShutdown = false",
        }
        
        applied = set()
        for line in lines:
            matched = False
            for key, val in patches.items():
                if line.strip().startswith(f"{key} =") or line.strip() == f"{key}=":
                    new_lines.append(val)
                    applied.add(key)
                    matched = True
                    break
            if not matched:
                new_lines.append(line)
        
        # Add any missing keys
        for key, val in patches.items():
            if key not in applied:
                new_lines.append(val)
        
        INI_PATH.write_text("\n".join(new_lines))
        log.info("PCSX2.ini patched (PINE, Fullscreen, NoWizard)")
    except Exception as exc:
        log.error("Failed to patch PCSX2.ini: %s", exc)


def _kill_pcsx2():
    """Stop any running pcsx2-qt instance."""
    with _session_lock:
        _session["is_managed"] = False
        if _session["process"] and _session["process"].poll() is None:
            log.info("Stopping managed PCSX2 process (PID %d)...", _session["process"].pid)
            _session["process"].terminate()
            try:
                _session["process"].wait(timeout=5)
            except subprocess.TimeoutExpired:
                _session["process"].kill()
        _session["process"] = None

    # Also kill any unmanaged instances just in case
    try:
        subprocess.run(["pkill", "-15", "-f", "pcsx2-qt"], capture_output=True)
        time.sleep(1)
        # Force kill if still there
        subprocess.run(["pkill", "-9", "-f", "pcsx2-qt"], capture_output=True)
    except Exception as exc:
        log.warning("Error during pkill: %s", exc)


def _monitor_process(proc):
    """Wait for the process to exit, then relaunch dashboard if still managed."""
    proc.wait()
    
    with _session_lock:
        # If we didn't intentionally kill it, relaunch into dashboard mode
        if _session["is_managed"] and _session["process"] == proc:
            log.info("PCSX2 exited (managed). Relaunching dashboard in 1s...")
            time.sleep(1)
            _session["rom_path"] = None
            _session["rom_name"] = "Dashboard"
            _session["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _launch_pcsx2_internal(None)


def _launch_pcsx2_internal(rom_path):
    """Launch pcsx2-qt via sudo -u abc. Assumes INI is already patched if needed."""
    
    # Pre-launch fix for Selkies sockets
    try:
        subprocess.run("chmod 666 /tmp/selkies* 2>/dev/null || true", shell=True)
    except:
        pass

    # Construct command
    cmd = [
        "sudo", "-u", "abc",
        "env",
        f"DISPLAY={ENV['DISPLAY']}",
        f"WAYLAND_DISPLAY={ENV['WAYLAND_DISPLAY']}",
        f"XDG_RUNTIME_DIR={ENV['XDG_RUNTIME_DIR']}",
        f"PULSE_RUNTIME_PATH={ENV['PULSE_RUNTIME_PATH']}",
        f"HOME={ENV['HOME']}",
        f"QT_QPA_PLATFORM={ENV['QT_QPA_PLATFORM']}",
        "pcsx2-qt", "-batch", "-fullscreen"
    ]
    
    if rom_path:
        cmd.append(rom_path)
    
    log.info("Launching: %s", " ".join(cmd))
    
    try:
        # Launch in background
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setpgrp # Create new process group
        )
        _session["process"] = proc
        _session["is_managed"] = True
        log.info("PCSX2 launched with PID %d", proc.pid)
        
        # Monitor for exit
        Thread(target=_monitor_process, args=(proc,), daemon=True).start()
    except Exception as exc:
        log.error("Failed to launch PCSX2: %s", exc)
        _session["process"] = None
        _session["is_managed"] = False


def _launch_pcsx2(rom_path):
    """Stop existing, patch, and launch new."""
    _kill_pcsx2()
    _patch_ini()
    time.sleep(1)
    
    with _session_lock:
        _session["rom_path"] = rom_path
        _session["rom_name"] = Path(rom_path).stem if rom_path else "Dashboard"
        _session["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _launch_pcsx2_internal(rom_path)


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
            with _session_lock:
                active = _session["process"] is not None and _session["process"].poll() is None
                self._send_json(200, {
                    "active": active,
                    "rom_path": _session["rom_path"] if active else None,
                    "rom_name": _session["rom_name"] if active else None,
                    "started_at": _session["started_at"] if active else None,
                })
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/launch":
            self._send_json(404, {"error": "not found"})
            return
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return

        body = self._read_body()
        rom_path = body.get("rom_path", "").strip()

        if not rom_path:
            self._send_json(400, {"error": "rom_path is required"})
            return

        if not Path(rom_path).exists():
            self._send_json(422, {"error": "rom_path does not exist", "path": rom_path})
            return

        Thread(target=_launch_pcsx2, args=(rom_path,), daemon=True).start()
        self._send_json(200, {"status": "launching", "rom_path": rom_path})

    def do_DELETE(self):
        if self.path != "/launch":
            self._send_json(404, {"error": "not found"})
            return
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return

        def _task():
            log.info("Soft reset: returning to dashboard")
            _launch_pcsx2(None)

        Thread(target=_task, daemon=True).start()
        self._send_json(200, {"status": "resetting"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Broker-Secret")
        self.end_headers()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
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
