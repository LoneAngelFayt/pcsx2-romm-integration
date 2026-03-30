#!/usr/bin/env python3
"""
broker.py — ROM launch broker for linuxserver/pcsx2 container
Drop into /config/broker.py inside the container.
"""

import json
import logging
import os
import pwd
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

# ── Config ────────────────────────────────────────────────────────────────────

PORT    = int(os.environ.get("BROKER_PORT", "8000"))
DISPLAY = os.environ.get("DISPLAY") or subprocess.run(
    ["bash", "-c", "ls /tmp/.X11-unix/ | head -1 | sed 's/X/:/'" ],
    capture_output=True, text=True
).stdout.strip() or ":0"
SECRET  = os.environ.get("BROKER_SECRET", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [broker] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("broker")

# ── Session state ─────────────────────────────────────────────────────────────

_session: dict = {}

# ── Process helpers ───────────────────────────────────────────────────────────

def _kill_pcsx2() -> None:
    """Graceful shutdown — SIGTERM first so PCSX2 can auto-save, SIGKILL fallback."""
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


def _launch_pcsx2(rom_path=None) -> None:
    """
    Launch PCSX2 as abc user with Selkies joystick interposer.
    rom_path=None launches the dashboard (soft reset — keeps stream alive).
    """
    time.sleep(1.0)
    subprocess.run(
        "chmod 666 /tmp/selkies_js*.sock /tmp/selkies_event*.sock 2>/dev/null || true",
        shell=True, check=False
    )

    pw = pwd.getpwnam("abc")
    env = {
        "DISPLAY": DISPLAY,
        "WAYLAND_DISPLAY": "wayland-0",
        "XDG_RUNTIME_DIR": "/config/.XDG",
        "XDG_CURRENT_DESKTOP": "wlroots",
        "HOME": "/config",
        "USER": "abc",
        "PATH": "/command:/lsiopy/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LD_PRELOAD": "/usr/lib/selkies_joystick_interposer.so:/opt/lib/libudev.so.1.0.0-fake",
        "PULSE_RUNTIME_PATH": "/defaults",
        "LANG": "en_US.UTF-8",
        "LANGUAGE": "en_US.UTF-8",
        "_JAVA_AWT_WM_NONREPARENTING": "1",
        "XCURSOR_SIZE": "24",
        "XCURSOR_THEME": "breeze",
        "TERM": "foot",
        "VIRTUAL_ENV": "/lsiopy",
        "PERL5LIB": "/usr/local/bin",
    }

    if rom_path:
        cmd = ["pcsx2-qt", "-batch", "-fullscreen", "--", rom_path]
        log.info("Launching PCSX2 with ROM: %s", rom_path)
    else:
        cmd = ["pcsx2-qt"]
        log.info("Launching PCSX2 dashboard (soft reset)")

    def _drop_to_abc():
        os.setgid(pw.pw_gid)
        os.setuid(pw.pw_uid)

    subprocess.Popen(
        cmd,
        env=env,
        preexec_fn=_drop_to_abc,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


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
            _kill_pcsx2()
            time.sleep(1.0)
            _launch_pcsx2(rom_path)
            _session.update({
                "rom_path": rom_path,
                "rom_name": rom_name,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
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
            log.info("Soft reset: stopping game, relaunching dashboard")
            _kill_pcsx2()
            time.sleep(1.0)
            _launch_pcsx2(rom_path=None)
            _session.clear()
            log.info("Soft reset complete")

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
    server = HTTPServer(("0.0.0.0", PORT), BrokerHandler)
    log.info("ROM broker listening on port %d", PORT)
    log.info("DISPLAY=%s", DISPLAY)
    if SECRET:
        log.info("Shared secret auth enabled")
    else:
        log.warning("No BROKER_SECRET set — unauthenticated access allowed")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
