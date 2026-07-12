#!/usr/bin/env python3
"""broker.py — launch PCSX2 on demand and expose a small HTTP API."""

import glob
import hmac
import io
import json
import logging
import os
import shutil
import signal
import socket as _socket
import struct
import subprocess
import sys
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from threading import Thread, Lock
from urllib.parse import parse_qs, urlparse

# ── Config ────────────────────────────────────────────────────────────────────

PORT     = int(os.environ.get("BROKER_PORT", "8000"))
SECRET   = os.environ.get("BROKER_SECRET", "")
ROM_ROOT = Path(os.environ.get("ROM_ROOT", "/romm/library")).resolve()

XDG_RUNTIME_DIR = "/config/.XDG"


def _live_x_sockets() -> list[Path]:
    """Return X11 sockets that have a listening peer, newest first.
    A bare /tmp/.X11-unix/X<N> file with no Xvfb behind it is a stale lock
    that must be skipped or the broker will hand pcsx2-qt a dead display."""
    candidates: list[tuple[float, Path]] = []
    try:
        for entry in os.listdir("/tmp/.X11-unix"):
            if not (entry.startswith("X") and entry[1:].isdigit()):
                continue
            p = Path("/tmp/.X11-unix") / entry
            try:
                st = p.stat()
                with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
                    s.settimeout(0.2)
                    s.connect(str(p))
                candidates.append((st.st_mtime, p))
            except OSError:
                continue
    except OSError:
        pass
    candidates.sort(reverse=True)
    return [p for _, p in candidates]


def _detect_display(default: str = ":0") -> str:
    """Return the live X display, preferring the most recently created socket.
    Falls back to $DISPLAY then `default` if no live socket can be probed."""
    live = _live_x_sockets()
    if live:
        return f":{live[0].name[1:]}"
    return os.environ.get("DISPLAY", default)


def _detect_wayland_display(default: str = "wayland-1") -> str:
    """Return the most recent wayland-* socket name in $XDG_RUNTIME_DIR.
    Falls back to $WAYLAND_DISPLAY then `default`."""
    runtime = Path(XDG_RUNTIME_DIR)
    try:
        socks = sorted(
            (p for p in runtime.iterdir() if p.name.startswith("wayland-") and p.is_socket()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if socks:
            return socks[0].name
    except OSError:
        pass
    return os.environ.get("WAYLAND_DISPLAY", default)


def _wait_for_x_display(timeout: float = 30.0) -> str | None:
    """Block until at least one live X socket appears, returning the display.
    Returns None on timeout. Used at startup instead of a fixed sleep so the
    broker doesn't race the desktop on slow hosts."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        live = _live_x_sockets()
        if live:
            return f":{live[0].name[1:]}"
        time.sleep(0.25)
    return None


# LD_PRELOAD must include both the joystick interposer and the fake libudev
# (the latter lets SDL discover the synthetic /dev/input/js* devices that the
# linuxserver init creates via mknod). The base image normally exports it; if
# it didn't, we fall back to the well-known paths but warn loudly so the
# operator sees that gamepads may be silently broken.
_LD_PRELOAD_DEFAULT = "/usr/lib/selkies_joystick_interposer.so:/opt/lib/libudev.so.1.0.0-fake"
_LD_PRELOAD = os.environ.get("LD_PRELOAD") or _LD_PRELOAD_DEFAULT
_LD_PRELOAD_FROM_ENV = "LD_PRELOAD" in os.environ and bool(os.environ["LD_PRELOAD"])

ENV = {
    "DISPLAY":           _detect_display(),
    "WAYLAND_DISPLAY":   _detect_wayland_display(),
    "XDG_RUNTIME_DIR":   XDG_RUNTIME_DIR,
    "PULSE_RUNTIME_PATH":"/defaults",
    "LD_PRELOAD":        _LD_PRELOAD,
    "HOME":              "/config",
    "USER":              "abc",
    "QT_QPA_PLATFORM":   "xcb",
}

INI_PATH = Path("/config/.config/PCSX2/inis/PCSX2.ini")

# Bounds the save-state write-confirmation poll (PINE and xdotool paths).
PINE_WAIT    = float(os.environ.get("PINE_WAIT",   "20.0"))   # max seconds to poll for write completion
SAVE_SLOT    = int(os.environ.get("SAVE_SLOT", "10"))
SSTATE_DIR   = Path(os.environ.get("SSTATE_DIR", "/config/.config/PCSX2/sstates"))

# Resume-from-state (launch with load_slot): how long to wait for the game VM
# to come up, and how long to let it settle before the deferred slot load.
RESUME_LOAD_WAIT   = float(os.environ.get("RESUME_LOAD_WAIT",   "90.0"))
RESUME_LOAD_SETTLE = float(os.environ.get("RESUME_LOAD_SETTLE", "3.0"))

logging.basicConfig(
    level=getattr(logging, os.environ.get("BROKER_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [broker] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("broker")

# Warn if the linuxserver init didn't export LD_PRELOAD — gamepads silently
# break when only the interposer is loaded without the fake libudev. Logged
# here (after `log` exists) rather than at module import.
if not _LD_PRELOAD_FROM_ENV:
    log.warning(
        "LD_PRELOAD not set in environment; falling back to %s. "
        "If the linuxserver image moved these libraries, gamepads will not appear in PCSX2.",
        _LD_PRELOAD_DEFAULT,
    )

# pcsx2-qt's own stdout/stderr is captured to this log so renderer/Vulkan/Qt
# errors are visible after the fact. /config is the only host-mapped writable
# path we can rely on.
PCSX2_LOG_PATH = Path(os.environ.get("PCSX2_LOG_PATH", "/config/pcsx2-qt.log"))

# UID/GID to chown the log to; defaults to the linuxserver abc user but honors
# PUID/PGID overrides so pcsx2-qt can still write it on a remapped container.
_LOG_UID = int(os.environ.get("PUID", "1000"))
_LOG_GID = int(os.environ.get("PGID", "1000"))

# ── Session state ─────────────────────────────────────────────────────────────

_session_lock = Lock()
_session: dict = {
    "process":          None,
    "rom_path":         None,
    "rom_name":         None,
    "started_at":       None,
    "is_managed":       False,
    "save_in_progress": False,
    "launch_in_progress": False,  # guards against concurrent /launch requests
    "current_slot":     1,      # tracks PCSX2's active save state slot (resets to 1 on each launch)
    # Wall-clock stamp of the last GAME launch (not dashboard relaunches).
    # GET /save-file only ships files modified at or after this point; it
    # survives game exit so RomM can still pull after the session ends.
    "save_baseline":    None,
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
    """Force broker-required PCSX2.ini settings. Each key is scoped to its
    expected section so identically-named keys in other sections aren't stomped.
    Failures are logged loudly — silent failure here means PCSX2 launches with
    the wrong PINE/Fullscreen/Shutdown settings and downstream features break."""
    if not INI_PATH.exists():
        log.warning("PCSX2.ini not found at %s — skipping patch", INI_PATH)
        return

    # (section, key) → "key = value" line.
    patches: dict[tuple[str, str], str] = {
        ("EmuCore",      "EnablePINE"):          "EnablePINE = true",
        ("UI",           "StartFullscreen"):     "StartFullscreen = true",
        ("UI",           "ConfirmShutdown"):     "ConfirmShutdown = false",
        ("EmuCore",      "SaveStateOnShutdown"): "SaveStateOnShutdown = false",
    }

    try:
        lines = INI_PATH.read_text().splitlines()
        section = ""
        applied: set[tuple[str, str]] = set()
        new_lines: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1]
                new_lines.append(line)
                continue
            matched = False
            for (sec, key), val in patches.items():
                if section != sec:
                    continue
                if stripped.startswith(f"{key} =") or stripped.startswith(f"{key}="):
                    new_lines.append(val)
                    applied.add((sec, key))
                    matched = True
                    break
            if not matched:
                new_lines.append(line)

        # Append missing keys under their target section header. If the section
        # doesn't exist at all we create it so PCSX2 picks the value up.
        missing = [(sec, key, val) for (sec, key), val in patches.items() if (sec, key) not in applied]
        if missing:
            present_sections = {l.strip()[1:-1] for l in new_lines if l.strip().startswith("[") and l.strip().endswith("]")}
            for sec, _key, val in missing:
                if sec in present_sections:
                    # Insert immediately after the section header.
                    out: list[str] = []
                    inserted = False
                    for l in new_lines:
                        out.append(l)
                        if not inserted and l.strip() == f"[{sec}]":
                            out.append(val)
                            inserted = True
                    new_lines = out
                else:
                    new_lines.extend(["", f"[{sec}]", val])
                    present_sections.add(sec)

        tmp = INI_PATH.with_suffix(".tmp")
        tmp.write_text("\n".join(new_lines) + "\n")
        tmp.replace(INI_PATH)  # atomic on POSIX; prevents partial-write corruption
        log.debug("PCSX2.ini patched (PINE, Fullscreen, NoConfirmShutdown, SaveStateOnShutdown)")
    except OSError as exc:
        log.error("PCSX2.ini patch failed (filesystem): %s — broker settings NOT applied", exc)
    except Exception:
        log.exception("PCSX2.ini patch failed unexpectedly — broker settings NOT applied")


def _read_initial_save_slot() -> int:
    """Best-effort read of PCSX2's last-used save slot from PCSX2.ini.

    PCSX2 persists the active save state slot in [EmuCore] under
    `SaveStateSlot`. If the key is absent or the file is unreadable, fall
    back to the BROKER_INITIAL_SLOT env var (default 1). Used by the launch
    handler to seed the slot tracker so xdotool save-state cycling targets
    the right slot on the first save after launch.
    """
    fallback = int(os.environ.get("BROKER_INITIAL_SLOT", "1"))
    if not INI_PATH.exists():
        return fallback
    try:
        section = ""
        for raw in INI_PATH.read_text().splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue
            if section != "EmuCore":
                continue
            if line.startswith("SaveStateSlot =") or line.startswith("SaveStateSlot="):
                _, _, value = line.partition("=")
                try:
                    n = int(value.strip())
                except ValueError:
                    return fallback
                if 1 <= n <= 10:
                    return n
                return fallback
    except OSError as exc:
        log.debug("Could not read SaveStateSlot from PCSX2.ini: %s", exc)
    return fallback


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
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.error("PCSX2 did not exit after SIGKILL — giving up")
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

    log.info("Launching PCSX2 (rom=%s)", rom_path or "dashboard")
    log.debug("Launching: %s", " ".join(cmd))

    # Open the pcsx2-qt log in append mode so we keep history across launches.
    # Failure to open it is non-fatal: emulator still launches, just without
    # captured output. We try to keep the file under abc:abc so pcsx2-qt can
    # write to it after sudo drops privileges.
    try:
        log_fh = open(PCSX2_LOG_PATH, "ab", buffering=0)
        try:
            os.chown(PCSX2_LOG_PATH, _LOG_UID, _LOG_GID)
        except (OSError, PermissionError):
            pass
        log_fh.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} launch (rom={rom_path or 'dashboard'}) ===\n".encode())
        log_fh.flush()
    except OSError as exc:
        log.warning("Cannot open %s for pcsx2-qt output capture (%s); continuing without capture.", PCSX2_LOG_PATH, exc)
        log_fh = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh if log_fh else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if log_fh else subprocess.DEVNULL,
            preexec_fn=os.setpgrp,  # own process group so killpg is clean
        )
    except OSError as exc:
        log.error("Failed to launch PCSX2: %s", exc)
        if log_fh:
            log_fh.close()
        with _session_lock:
            _session["process"] = None
            _session["is_managed"] = False
        return
    finally:
        # Popen dup'd the fd; we can close our handle so it's not leaked.
        if log_fh:
            log_fh.close()

    initial_slot = _read_initial_save_slot()
    with _session_lock:
        _session["process"] = proc
        _session["is_managed"] = True
        # PCSX2's persisted slot from PCSX2.ini, or BROKER_INITIAL_SLOT.
        # We can't query PCSX2 for its live slot, so this is a best-effort seed
        # that tracks the same value PCSX2 itself loads on startup.
        _session["current_slot"] = initial_slot
    log.info("PCSX2 launched (PID %d, initial save slot %d)", proc.pid, initial_slot)
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
        # Dashboard is an idle state, not a session; clearing started_at lets
        # /status reliably distinguish "playing" from "not playing".
        _session["started_at"] = None

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
        log.debug("Socket drain: no gamepad sockets found.")
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

    log.debug(
        "Socket drain: sent EOF to %d socket(s), removed %d dead file(s) (of %d total).",
        drained, removed, len(paths),
    )


def _wait_for_no_pcsx2(timeout: float = 3.0) -> bool:
    """Block until no `pcsx2-qt` process is running, up to `timeout` seconds.
    Returns True if the process is gone, False on timeout. This replaces a
    fixed sleep that papered over residual processes after _kill_pcsx2."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(["pgrep", "-x", "pcsx2-qt"], capture_output=True)
        if result.returncode != 0:  # pgrep exits 1 when no process matches
            return True
        time.sleep(0.1)
    return False


def _launch_pcsx2(rom_path, release_claim=False):
    try:
        _kill_pcsx2()
        _drain_gamepad_sockets()
        _patch_ini()
        if not _wait_for_no_pcsx2():
            # _kill_pcsx2 already reaped the managed process group, so any
            # survivor is an unmanaged stray. Strays sit on top of the new
            # game window, steal xdotool targeting, and unlink the live
            # instance's PINE socket when they finally exit — reap them.
            log.warning("Stray pcsx2-qt still running after kill+drain — sending SIGKILL")
            subprocess.run(["pkill", "-9", "-x", "pcsx2-qt"], capture_output=True)
            if not _wait_for_no_pcsx2():
                log.error("Stray pcsx2-qt survived SIGKILL; new instance may misbehave")
        with _session_lock:
            _session["rom_path"] = rom_path
            _session["rom_name"] = Path(rom_path).stem if rom_path else "Dashboard"
            # Only stamp a session start when an actual ROM is being played. The
            # dashboard is an idle state.
            _session["started_at"] = (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if rom_path else None
            )
            # Dashboard relaunches keep the previous baseline so an end-of-session
            # save pull still sees the files the last game wrote.
            if rom_path:
                _session["save_baseline"] = time.time()
        _launch_pcsx2_internal(rom_path)
    finally:
        # Only the caller that claimed launch_in_progress (POST /launch) may
        # release it — a dashboard relaunch clearing it unconditionally would
        # wipe a concurrent /launch's claim and reopen the TOCTOU.
        if release_claim:
            with _session_lock:
                _session["launch_in_progress"] = False


def _sstate_snapshot() -> dict:
    """Return {Path: (size, mtime)} for every .p2s file currently in SSTATE_DIR."""
    if not SSTATE_DIR.is_dir():
        log.debug("Save: SSTATE_DIR absent — %s", SSTATE_DIR)
        return {}
    snap = {}
    for p in SSTATE_DIR.glob("*.p2s"):
        try:
            st = p.stat()
            snap[p] = (st.st_size, st.st_mtime)
        except OSError:
            pass
    log.debug("Save: snapshot — %d file(s) in %s", len(snap), SSTATE_DIR)
    return snap


def _wait_for_sstate_write(before: dict, deadline: float) -> bool:
    """Poll SSTATE_DIR until a save state write completes or deadline is reached.

    Detects both new files and overwrites of existing ones (by mtime change).
    Once a target file is found, waits for its size to be stable for 0.5 s
    before returning — handles both direct writes and atomic rename patterns.

    Returns True if a completed write was detected, False if deadline elapsed.
    """
    STABLE_SECS  = 0.5
    POLL_SECS    = 0.1
    start        = time.monotonic()
    target: Path | None = None
    last_size: int | None = None
    stable_since: float | None = None

    while time.monotonic() < deadline:
        after = _sstate_snapshot()

        if target is None:
            for p, (size, mtime) in after.items():
                prev = before.get(p)
                if prev is None or prev[1] != mtime:
                    target = p
                    last_size = size
                    stable_since = time.monotonic()
                    log.debug("Save: write detected — %s (%d bytes, mtime %.3f)", p.name, size, mtime)
                    break
                else:
                    log.debug("Save: %s unchanged (mtime %.3f)", p.name, mtime)
        else:
            cur = after.get(target)
            if cur is None:
                # File disappeared mid-write (shouldn't happen, but reset)
                target = None
            else:
                cur_size = cur[0]
                if cur_size != last_size:
                    last_size = cur_size
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= STABLE_SECS:  # type: ignore[operator]
                    log.info(
                        "Save: state write complete — %s (%d bytes) in %.1fs",
                        target.name, last_size,
                        time.monotonic() - start,
                    )
                    return True

        time.sleep(POLL_SECS)

    return False


# ── State file transfer ───────────────────────────────────────────────────────
# RomM's backend syncs save states through these helpers: after a save it
# GETs the freshly written slot file, and on session claim it PUTs the user's
# stored states back into SSTATE_DIR. GET blocks while a save is in flight so
# the response always carries the completed write.

STATE_FILE_MAX_BYTES = 256 * 1024 * 1024
STATE_GET_WAIT = float(os.environ.get("STATE_GET_WAIT", "30.0"))


def _wait_for_save_idle(deadline: float) -> None:
    """Block until no save is in flight, or the deadline passes."""
    while time.monotonic() < deadline:
        with _session_lock:
            if not _session["save_in_progress"]:
                return
        time.sleep(0.2)


def _newest_state_for_slot(slot: int) -> Path | None:
    """Newest .p2s for the slot. PCSX2 names states `{serial} ({crc}).{slot:02d}.p2s`;
    the bare-suffix glob covers older unpadded names."""
    if not SSTATE_DIR.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for pattern in {f"*.{slot:02d}.p2s", f"*.{slot}.p2s"}:
        for p in SSTATE_DIR.glob(pattern):
            try:
                candidates.append((p.stat().st_mtime, p))
            except OSError:
                pass
    if not candidates:
        return None
    return max(candidates)[1]


# ── PINE IPC ──────────────────────────────────────────────────────────────────
# PCSX2's native IPC protocol. _patch_ini enables it (EnablePINE = true) and
# pcsx2-qt listens on a unix socket in XDG_RUNTIME_DIR. Used as the primary
# save/load-state channel: xdotool F-key delivery is unreliable against
# pcsx2-qt (both XSendEvent and XTEST presses are dropped), so keypresses are
# only a fallback for the window between launch and PINE socket creation.

PINE_SOCKET = Path(XDG_RUNTIME_DIR) / "pcsx2.sock"

_PINE_MSG_SAVE_STATE = 0x09
_PINE_MSG_LOAD_STATE = 0x0A
_PINE_MSG_EMU_STATUS = 0x0F


def _pine_recv_exact(sock: _socket.socket, n: int) -> bytes | None:
    """Read exactly n bytes, or None if the peer closes early."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _pine_request(opcode: int, payload: bytes = b"", timeout: float = 5.0) -> bytes | None:
    """Send one PINE request and return the reply payload, or None on failure.

    Wire format (little-endian): request is u32 total packet size (including
    the size field itself), u8 opcode, then payload. Reply is u32 size,
    u8 result code (0 = OK), then reply payload.
    """
    packet = struct.pack("<IB", 5 + len(payload), opcode) + payload
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(PINE_SOCKET))
            sock.sendall(packet)
            header = _pine_recv_exact(sock, 5)
            if header is None:
                log.warning("PINE: connection closed before reply header (opcode 0x%02X)", opcode)
                return None
            size, result = struct.unpack("<IB", header)
            body = b""
            if size > 5:
                body = _pine_recv_exact(sock, size - 5) or b""
            if result != 0:
                log.warning("PINE: opcode 0x%02X rejected (result %d)", opcode, result)
                return None
            return body
    except OSError as exc:
        # socket.timeout is an OSError subclass, so this covers timeouts too.
        log.warning("PINE: request failed on %s (opcode 0x%02X): %s", PINE_SOCKET, opcode, exc)
        return None


def _pine_save_state(slot: int) -> bool | None:
    """Save state to `slot` via PINE.

    Returns True on a confirmed state file write, False if PCSX2 accepted the
    command but no write appeared within PINE_WAIT, and None if the PINE
    request itself failed (caller should fall back to xdotool).
    """
    before = _sstate_snapshot()
    if _pine_request(_PINE_MSG_SAVE_STATE, bytes([slot])) is None:
        return None
    # PCSX2 queues the save onto the VM thread and replies immediately, so
    # confirm the actual write the same way the xdotool path does.
    log.info("PINE: save state accepted (slot %d) — waiting for write (max %.1fs)", slot, PINE_WAIT)
    if _wait_for_sstate_write(before, time.monotonic() + PINE_WAIT):
        return True
    log.warning("PINE: save accepted but no state file write within %.1fs (slot %d)", PINE_WAIT, slot)
    return False


def _save_state(slot: int) -> bool:
    """Save state via PINE, falling back to xdotool keypresses."""
    result = _pine_save_state(slot)
    if result is not None:
        return result
    log.warning("PINE unavailable — falling back to xdotool F-key delivery")
    return _xdotool_save_state(slot)


def _load_state(slot: int) -> bool:
    """Load state via PINE, falling back to xdotool keypresses."""
    if _pine_request(_PINE_MSG_LOAD_STATE, bytes([slot])) is not None:
        log.info("PINE: load state accepted (slot %d)", slot)
        return True
    log.warning("PINE unavailable — falling back to xdotool F-key delivery")
    return _xdotool_load_state(slot)


def _pine_emu_status() -> int | None:
    """VM status via PINE: 0 running, 1 paused, 2 shutdown. None if PINE is down."""
    body = _pine_request(_PINE_MSG_EMU_STATUS, timeout=2.0)
    if body is None or len(body) < 4:
        return None
    return struct.unpack("<I", body[:4])[0]


def _deferred_load_state(slot: int) -> None:
    """Resume-from-state: load `slot` once the freshly launched game's VM is up.

    Waits for the launch to finish swapping instances before trusting the
    status probe — probing earlier could see a still-running previous game
    and load the slot into the wrong VM. After the swap, only the new game
    VM reports running (a gameless relaunch reports shutdown).
    """
    deadline = time.monotonic() + RESUME_LOAD_WAIT
    while time.monotonic() < deadline:
        with _session_lock:
            launching = _session["launch_in_progress"]
        if not launching and _pine_emu_status() == 0:
            time.sleep(RESUME_LOAD_SETTLE)
            ok = _load_state(slot)
            log.info("resume: deferred load of slot %d %s", slot, "delivered" if ok else "failed")
            return
        time.sleep(1.0)
    log.warning("resume: VM never reached running state — slot %d not loaded", slot)


_XDOTOOL_ENV = {
    "DISPLAY":         ENV["DISPLAY"],
    "HOME":            "/config",
    "USER":            "abc",
    "XDG_RUNTIME_DIR": ENV["XDG_RUNTIME_DIR"],
}


def _xdotool_find_window() -> str | None:
    """Return the X11 window ID for pcsx2-qt, or None if not found."""
    try:
        pids = subprocess.check_output(
            ["pgrep", "-x", "pcsx2-qt"], text=True
        ).split()
    except subprocess.CalledProcessError:
        log.error("xdotool: pcsx2-qt process not found")
        return None

    xdo_base = (
        ["sudo", "-u", "abc", "env"]
        + [f"{k}={v}" for k, v in _XDOTOOL_ENV.items()]
        + ["xdotool"]
    )

    last_search_err: str | None = None
    for pid in pids:
        try:
            out = subprocess.check_output(
                xdo_base + ["search", "--onlyvisible", "--pid", pid], text=True, timeout=5,
            )
            ids = out.strip().split()
            if ids:
                wid = ids[-1]  # last window is the main game surface
                log.debug("xdotool: found window %s for PID %s", wid, pid)
                return wid
        except subprocess.CalledProcessError as exc:
            # Non-zero from xdotool = no match for this PID; expected during
            # the brief period before the main window is mapped.
            last_search_err = f"PID {pid}: exit {exc.returncode}"
        except subprocess.TimeoutExpired:
            log.warning("xdotool: search timed out for PID %s (X server slow or hung)", pid)
        except OSError as exc:
            log.warning("xdotool: failed to invoke for PID %s: %s", pid, exc)

    # Fallback: search by class name (case where window has no _NET_WM_PID).
    try:
        out = subprocess.check_output(
            xdo_base + ["search", "--onlyvisible", "--classname", "pcsx2-qt"], text=True, timeout=5,
        )
        ids = out.strip().split()
        if ids:
            wid = ids[-1]
            log.debug("xdotool: found window %s by classname fallback", wid)
            return wid
    except subprocess.CalledProcessError as exc:
        last_search_err = f"classname: exit {exc.returncode}"
    except subprocess.TimeoutExpired:
        log.warning("xdotool: classname search timed out (X server slow or hung)")
    except OSError as exc:
        log.warning("xdotool: classname fallback failed to invoke: %s", exc)

    log.warning(
        "xdotool: PCSX2 window not found (PIDs %s, last error: %s) — F-key shortcuts will not be delivered",
        pids, last_search_err,
    )
    return None


def _xdotool_cycle_to_slot(wid: str, slot: int) -> bool:
    """Cycle PCSX2's active save slot to `slot` using F2 / Shift+F2.

    Updates _session["current_slot"] after confirmed key delivery.
    Returns False if any keypress fails.
    """
    effective_slot = slot if 1 <= slot <= 10 else 1
    with _session_lock:
        tracked = _session["current_slot"]
    fwd = (effective_slot - tracked) % 10
    bwd = (tracked - effective_slot) % 10
    # Prefer backward (Shift+F2) on a tie to minimise visible OSD cycling.
    if bwd <= fwd:
        key, cycles = "shift+F2", bwd
    else:
        key, cycles = "F2", fwd

    xdo_cmd = (
        ["sudo", "-u", "abc", "env"]
        + [f"{k}={v}" for k, v in _XDOTOOL_ENV.items()]
        + ["xdotool"]
    )
    # Track slot position incrementally so a partial failure leaves
    # current_slot reflecting how far we actually got.
    current = tracked
    for _ in range(cycles):
        try:
            subprocess.run(
                xdo_cmd + ["key", "--window", wid, key], timeout=5, check=True
            )
        except subprocess.CalledProcessError as exc:
            log.error("xdotool: slot cycle keypress failed (exit %d, stderr=%s)", exc.returncode, exc.stderr)
            with _session_lock:
                _session["current_slot"] = current
            return False
        except subprocess.TimeoutExpired:
            log.error("xdotool: slot cycle keypress timed out (window %s, key %s)", wid, key)
            with _session_lock:
                _session["current_slot"] = current
            return False
        except OSError as exc:
            log.error("xdotool: failed to invoke for slot cycle: %s", exc)
            with _session_lock:
                _session["current_slot"] = current
            return False
        # Advance current by one step in the chosen direction (1-based, wraps 1..10).
        if key == "F2":
            current = current % 10 + 1       # forward: 10 → 1, n → n+1
        else:
            current = (current - 2) % 10 + 1  # backward: 1 → 10, n → n-1
        time.sleep(0.05)

    with _session_lock:
        _session["current_slot"] = effective_slot
    return True


def _xdotool_save_state(slot: int) -> bool:
    """Save emulator state by sending keypresses to the PCSX2 window via xdotool.

    PCSX2 has no direct "save to slot N" shortcut. F1 saves to the current slot
    (default slot 1 on launch) and F2 cycles the current slot forward. This
    function presses F2 (slot-1) times to reach the target slot, then F1 to save.

    Must run as abc (X11 auth). xdotool targets the window by PID so focus state
    doesn't matter. The end user's own F-key presses are unaffected.
    """
    wid = _xdotool_find_window()
    if wid is None:
        return False

    if not _xdotool_cycle_to_slot(wid, slot):
        return False

    before = _sstate_snapshot()

    xdo_cmd = (
        ["sudo", "-u", "abc", "env"]
        + [f"{k}={v}" for k, v in _XDOTOOL_ENV.items()]
        + ["xdotool"]
    )
    try:
        subprocess.run(xdo_cmd + ["key", "--window", wid, "F1"], timeout=5, check=True)
    except subprocess.CalledProcessError as exc:
        log.error("xdotool: F1 send failed (exit %d, stderr=%s)", exc.returncode, exc.stderr)
        return False
    except subprocess.TimeoutExpired:
        log.error("xdotool: F1 send timed out on window %s", wid)
        return False
    except OSError as exc:
        log.error("xdotool: failed to invoke for F1: %s", exc)
        return False

    log.info(
        "xdotool: F1 sent to window %s (slot %d) — waiting for write (max %.1fs)",
        wid, slot, PINE_WAIT,
    )
    deadline = time.monotonic() + PINE_WAIT
    if not _wait_for_sstate_write(before, deadline):
        log.warning("xdotool: save state write not confirmed within %.1fs (F1 was sent)", PINE_WAIT)
    return True  # F1 was delivered; write detection is best-effort confirmation


def _xdotool_load_state(slot: int) -> bool:
    """Load emulator state by cycling to slot and pressing F3."""
    wid = _xdotool_find_window()
    if wid is None:
        return False

    if not _xdotool_cycle_to_slot(wid, slot):
        return False

    xdo_cmd = (
        ["sudo", "-u", "abc", "env"]
        + [f"{k}={v}" for k, v in _XDOTOOL_ENV.items()]
        + ["xdotool"]
    )
    try:
        subprocess.run(xdo_cmd + ["key", "--window", wid, "F3"], timeout=5, check=True)
    except subprocess.CalledProcessError as exc:
        log.error("xdotool: F3 send failed (exit %d, stderr=%s)", exc.returncode, exc.stderr)
        return False
    except subprocess.TimeoutExpired:
        log.error("xdotool: F3 send timed out on window %s", wid)
        return False
    except OSError as exc:
        log.error("xdotool: failed to invoke for F3: %s", exc)
        return False

    log.info("xdotool: F3 sent to window %s (slot %d)", wid, slot)
    return True


def _save_and_exit(slot: int) -> bool:
    """Save emulator state then kill PCSX2. Returns True if save succeeded."""
    ok = _save_state(slot)
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


_PACTL_CMD = [
    "sudo", "-u", "abc", "env",
    "PULSE_RUNTIME_PATH=/defaults",
    "HOME=/config",
    "USER=abc",
]


def _pactl(*args: str) -> subprocess.CompletedProcess:
    """Run pactl as abc so it connects to abc's PulseAudio instance.

    A hung or missing pactl is reported as a non-zero CompletedProcess (rather
    than raising) so the /volume and /mute handlers return a 500 instead of
    dropping the connection with an unhandled exception."""
    cmd = _PACTL_CMD + ["pactl"] + list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        log.error("pactl timed out: %s", " ".join(args))
        return subprocess.CompletedProcess(cmd, 124, "", "pactl timed out")
    except OSError as exc:
        log.error("pactl failed to run: %s", exc)
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _pactl_get_volume() -> int | None:
    """Return current sink volume as an integer 0–100, or None on error."""
    result = _pactl("get-sink-volume", "@DEFAULT_SINK@")
    if result.returncode != 0:
        return None
    # Output: "Volume: front-left: 65536 / 100% / 0.00 dB, ..."
    for part in result.stdout.split():
        if part.endswith("%"):
            try:
                return int(part.rstrip("%"))
            except ValueError:
                pass
    return None


def _pactl_get_mute() -> bool | None:
    """Return current mute state as bool, or None on error."""
    result = _pactl("get-sink-mute", "@DEFAULT_SINK@")
    if result.returncode != 0:
        log.error("pactl get-sink-mute failed (rc=%s): %s",
                  result.returncode, result.stderr.strip())
        return None
    return result.stdout.strip().endswith("yes")


# ── In-game save sync ─────────────────────────────────────────────────────────
# Alongside the savestate sync above, RomM syncs PCSX2's memory cards. GET
# /save-file zips every memcard file modified since the last game launch; PUT
# /save-file restores a pulled archive before launch. The mtime last-write-wins
# guard is what keeps an old pulled card from clobbering a newer local one.
_SAVE_DATA_ROOTS = (
    Path("/config/.config/PCSX2"),
)
# Archive members must live under one of these root-relative subtrees; PUT
# rejects anything else so a crafted zip cannot reach configs or savestates.
SAVE_SYNC_SUBTREES = ("memcards",)
SAVE_FILE_MAX_BYTES = 256 * 1024 * 1024
# Zip stores mtimes at 2 s DOS resolution; the slack keeps the newer-file
# guard from skipping files over rounding alone.
_SAVE_MTIME_SLACK = 2.0


def _save_data_root() -> Path | None:
    """Return PCSX2's config dir, honouring a SAVE_DATA_ROOT override."""
    env = os.environ.get("SAVE_DATA_ROOT")
    if env:
        return Path(env)
    for c in _SAVE_DATA_ROOTS:
        if c.is_dir():
            return c
    return None


def _iter_save_files(root: Path) -> list[Path]:
    """Every regular file under the allowed save subtrees, sorted for a
    deterministic archive (identical content zips to identical bytes)."""
    files: list[Path] = []
    for sub in SAVE_SYNC_SUBTREES:
        base = root / sub
        if not base.is_dir():
            continue
        files.extend(
            p for p in sorted(base.rglob("*")) if p.is_file() and not p.is_symlink()
        )
    return files


def _build_save_archive(baseline: float) -> bytes | None:
    """Zip every save file modified since the last game launch.

    Returns None when there is nothing to sync — no data dir yet, or no file
    changed since `baseline`. Member paths are relative to the data dir so a
    later PUT restores them regardless of which candidate dir is live."""
    root = _save_data_root()
    if root is None:
        log.debug("save-file: no PCSX2 data dir found")
        return None
    changed: list[Path] = []
    total = 0
    for p in _iter_save_files(root):
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime >= baseline:
            changed.append(p)
            total += st.st_size
    if not changed:
        return None
    if total > SAVE_FILE_MAX_BYTES:
        log.warning("save-file: changed saves exceed size limit (%d bytes)", total)
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in changed:
            try:
                zf.write(p, p.relative_to(root).as_posix())
            except OSError as exc:
                log.warning("save-file: could not read %s — %s", p, exc)
    return buf.getvalue()


def _mkdirs_owned(path: Path) -> None:
    """mkdir -p with abc ownership on every directory this call creates, so
    PCSX2 (running as abc) can keep writing saves inside them later."""
    missing: list[Path] = []
    cur = path
    while not cur.exists():
        missing.append(cur)
        cur = cur.parent
    path.mkdir(parents=True, exist_ok=True)
    for d in reversed(missing):
        try:
            os.chown(d, _LOG_UID, _LOG_GID)
        except OSError:
            pass


def _extract_save_archive(content: bytes) -> tuple[int, int] | str:
    """Restore a pulled save archive into the data dir.

    Returns (written, skipped) on success, or an error string for a bad
    archive. Existing files newer than their archive member are skipped so a
    restore can never roll back saves made since the archive was taken."""
    root = _save_data_root() or _SAVE_DATA_ROOTS[0]
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return "body is not a zip archive"
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > SAVE_FILE_MAX_BYTES:
            return "archive exceeds size limit when extracted"
        for info in infos:
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts:
                return f"archive member escapes save dir: {info.filename}"
            if not any(
                member.as_posix().startswith(sub + "/") for sub in SAVE_SYNC_SUBTREES
            ):
                return f"archive member outside save subtrees: {info.filename}"

        written = skipped = 0
        for info in infos:
            target = root / PurePosixPath(info.filename)
            mtime = time.mktime(info.date_time + (0, 0, -1))
            try:
                if (
                    target.exists()
                    and target.stat().st_mtime > mtime + _SAVE_MTIME_SLACK
                ):
                    skipped += 1
                    continue
                _mkdirs_owned(target.parent)
                tmp = target.parent / f".{target.name}.tmp"
                tmp.write_bytes(zf.read(info))
                os.chown(tmp, _LOG_UID, _LOG_GID)
                os.replace(tmp, target)
                os.utime(target, (mtime, mtime))
            except OSError as exc:
                return f"could not write {info.filename}: {exc}"
            written += 1
    return (written, skipped)


# ── Whole memory-card sync (per-user card model) ──────────────────────────────
# Distinct from the /save-file mtime-merge path above: /memory-card ships and
# replaces the ENTIRE Slot-1 folder card (superblock + every game folder) as one
# host-independent image, so a user's card can be hydrated onto any pooled
# container. GET evacuates the whole card; PUT wipes Slot 1 and lays the card
# back down. Only Slot 1 is ever touched — Slot 2 is never synced.


def _memcards_dir() -> Path:
    return (_save_data_root() or _SAVE_DATA_ROOTS[0]) / "memcards"


def _slot1_filename() -> str | None:
    """Read [MemoryCards] Slot1_Filename from PCSX2.ini (manual parse, matching
    _read_initial_save_slot). Returns the configured card name, or None."""
    if not INI_PATH.exists():
        return None
    try:
        section = ""
        for raw in INI_PATH.read_text().splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue
            if section != "MemoryCards":
                continue
            if line.replace(" ", "").startswith("Slot1_Filename="):
                _, _, value = line.partition("=")
                return value.strip() or None
    except OSError as exc:
        log.debug("Could not read Slot1_Filename from PCSX2.ini: %s", exc)
    return None


def _slot1_card_path() -> Path | None:
    """Absolute path to the Slot-1 memory card, existence-independent. None when
    no Slot1_Filename is configured. A folder card is a DIRECTORY at this path; a
    File card is a regular .ps2 file (rejected by the whole-card sync). The INI
    value is a card name, so only its basename is honoured."""
    name = _slot1_filename()
    if not name:
        return None
    return _memcards_dir() / Path(name).name


def _build_memory_card_archive() -> bytes | None | str:
    """Zip the entire Slot-1 folder card, member paths relative to the card root
    so the image is card-name independent. Returns the zip bytes, None when
    Slot 1 has no folder card, or an error string when it is a File card."""
    path = _slot1_card_path()
    if path is None:
        return None
    if path.exists() and not path.is_dir():
        return "slot 1 is a File memory card; a Folder card is required"
    if not path.is_dir():
        return None
    files = [p for p in sorted(path.rglob("*")) if p.is_file() and not p.is_symlink()]
    total = 0
    for p in files:
        try:
            total += p.stat().st_size
        except OSError:
            continue
    if total > SAVE_FILE_MAX_BYTES:
        log.warning("memory-card: card exceeds size limit (%d bytes)", total)
        return "memory card exceeds size limit"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            try:
                zf.write(p, p.relative_to(path).as_posix())
            except OSError as exc:
                log.warning("memory-card: could not read %s — %s", p, exc)
    return buf.getvalue()


def _replace_memory_card(content: bytes) -> tuple[int] | str:
    """Wipe the Slot-1 folder card and lay down the pulled card image. Returns
    (written,) on success or an error string. The whole card is replaced (no
    per-file mtime merge): this is the hydrate-with-isolation guarantee for
    pooled containers. Extraction goes to a staging dir that is swapped over the
    live card, so a mid-way failure never leaves a half-wiped card."""
    path = _slot1_card_path()
    if path is None:
        return "no Slot 1 memory card configured in PCSX2.ini"
    if path.exists() and not path.is_dir():
        # This container is misprovisioned: hydrate needs a Folder card in slot 1.
        # Log loudly so the offending host surfaces in monitoring, then refuse.
        log.warning(
            "memory-card: hydrate rejected on %s — slot 1 is a File card at %s",
            _socket.gethostname(),
            path,
        )
        return "slot 1 is a File memory card; a Folder card is required"
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        return "body is not a zip archive"
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > SAVE_FILE_MAX_BYTES:
            return "archive exceeds size limit when extracted"
        for info in infos:
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts:
                return f"archive member escapes card dir: {info.filename}"

        parent = path.parent
        staging = parent / f".{path.name}.new-{os.getpid()}"
        backup = parent / f".{path.name}.old-{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            _mkdirs_owned(staging)
            written = 0
            for info in infos:
                target = staging / PurePosixPath(info.filename)
                _mkdirs_owned(target.parent)
                tmp = target.parent / f".{target.name}.tmp"
                tmp.write_bytes(zf.read(info))
                os.chown(tmp, _LOG_UID, _LOG_GID)
                os.replace(tmp, target)
                written += 1
            # Swap staging over the live card: move the old card aside, move the
            # new one into place, then drop the old. Both live under memcards/,
            # so the renames are atomic same-filesystem operations.
            if path.exists():
                os.replace(path, backup)
            os.replace(staging, path)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            # If we moved the old card aside but never put the new one in place,
            # restore it so a mid-swap failure never leaves the user cardless.
            if not path.exists() and backup.exists():
                try:
                    os.replace(backup, path)
                except OSError:
                    log.error("memory-card: could not restore card to %s", path)
            return f"could not write memory card: {exc}"
        finally:
            shutil.rmtree(backup, ignore_errors=True)
    return (written,)


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
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> dict:
        try:
            length = max(0, min(int(self.headers.get("Content-Length", 0)), 64 * 1024))
        except ValueError:
            length = 0
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def _get_state_file(self):
        query = parse_qs(urlparse(self.path).query)
        try:
            slot = int(query.get("slot", ["0"])[0])
        except ValueError:
            self._send_json(400, {"error": "slot must be an integer"})
            return
        if slot == 0:
            slot = SAVE_SLOT
        if not (1 <= slot <= 10):
            self._send_json(400, {"error": "slot must be 0–10"})
            return

        # Block while a save is being written so the caller never receives a
        # half-written or stale file right after triggering a save.
        _wait_for_save_idle(time.monotonic() + STATE_GET_WAIT)

        state_path = _newest_state_for_slot(slot)
        if state_path is None:
            self._send_json(404, {"error": "no state file for slot", "slot": slot})
            return
        try:
            content = state_path.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"could not read state file: {exc}"})
            return
        if len(content) > STATE_FILE_MAX_BYTES:
            self._send_json(413, {"error": "state file exceeds size limit"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-State-Filename", state_path.name)
        self.end_headers()
        self.wfile.write(content)
        log.info("state-file: served %s (%d bytes)", state_path.name, len(content))

    def _get_save_file(self):
        with _session_lock:
            baseline = _session["save_baseline"]
            rom_name = _session["rom_name"]
        if baseline is None:
            self._send_json(404, {"error": "no game has been launched"})
            return
        archive = _build_save_archive(baseline)
        if archive is None:
            self._send_json(404, {"error": "no save changes since last launch"})
            return
        # Header values must be latin-1; ROM stems can be anything.
        safe_name = "".join(
            c for c in (rom_name or "pcsx2") if c.isascii() and c.isprintable()
        ).strip() or "pcsx2"
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(archive)))
        self.send_header("X-Save-Filename", f"{safe_name}.saves.zip")
        self.end_headers()
        self.wfile.write(archive)
        log.info("save-file: served archive (%d bytes)", len(archive))

    def _put_save_file(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"error": "missing or empty body"})
            return
        if length > SAVE_FILE_MAX_BYTES:
            self._send_json(413, {"error": "archive too large"})
            return
        content = self.rfile.read(length)
        result = _extract_save_archive(content)
        if isinstance(result, str):
            self._send_json(400, {"error": result})
            return
        written, skipped = result
        log.info("save-file: restored archive — %d written, %d skipped", written, skipped)
        self._send_json(200, {"status": "ok", "written": written, "skipped": skipped})

    def _get_memory_card(self):
        result = _build_memory_card_archive()
        if isinstance(result, str):
            # e.g. slot 1 is a File card, not a Folder card.
            self._send_json(409, {"error": result})
            return
        if result is None:
            self._send_json(404, {"error": "no folder memory card in slot 1"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(result)))
        self.send_header("X-Memory-Card-Slot", "1")
        self.end_headers()
        self.wfile.write(result)
        log.info("memory-card: served slot-1 card (%d bytes)", len(result))

    def _put_memory_card(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"error": "missing or empty body"})
            return
        if length > SAVE_FILE_MAX_BYTES:
            self._send_json(413, {"error": "archive too large"})
            return
        content = self.rfile.read(length)
        if len(content) != length:
            self._send_json(400, {"error": "truncated request body"})
            return
        result = _replace_memory_card(content)
        if isinstance(result, str):
            self._send_json(400, {"error": result})
            return
        (written,) = result
        log.info("memory-card: replaced slot-1 card — %d files", written)
        self._send_json(200, {"status": "ok", "written": written})

    def do_PUT(self):
        if not self._check_secret():
            self._send_json(403, {"error": "forbidden"})
            return
        parsed = urlparse(self.path)
        if parsed.path == "/save-file":
            self._put_save_file()
            return
        if parsed.path == "/memory-card":
            self._put_memory_card()
            return
        if parsed.path != "/state-file":
            self._send_json(404, {"error": "not found"})
            return

        # Basename only — the filename came from a previous GET and is written
        # back verbatim so PCSX2 recognises the slot; path parts are rejected.
        raw_name = parse_qs(parsed.query).get("filename", [""])[0]
        filename = Path(raw_name).name
        if not filename or filename.startswith(".") or not filename.endswith(".p2s"):
            self._send_json(400, {"error": "filename must be a .p2s basename"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"error": "missing or invalid Content-Length"})
            return
        if length > STATE_FILE_MAX_BYTES:
            self._send_json(413, {"error": "state file exceeds size limit"})
            return
        content = self.rfile.read(length)
        if len(content) != length:
            self._send_json(400, {"error": "truncated request body"})
            return

        tmp = SSTATE_DIR / f".{filename}.tmp"
        try:
            SSTATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(content)
            # PCSX2 runs as abc and must be able to overwrite the slot later.
            os.chown(tmp, _LOG_UID, _LOG_GID)
            os.replace(tmp, SSTATE_DIR / filename)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            self._send_json(500, {"error": f"could not write state file: {exc}"})
            return
        log.info("state-file: stored %s (%d bytes)", filename, length)
        self._send_json(200, {"status": "ok", "filename": filename})

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        elif not self._check_secret():
            # /health stays open for container healthchecks; all other GETs
            # require the shared secret, matching POST/DELETE.
            self._send_json(403, {"error": "forbidden"})
        elif urlparse(self.path).path == "/state-file":
            self._get_state_file()
        elif urlparse(self.path).path == "/save-file":
            self._get_save_file()
        elif urlparse(self.path).path == "/memory-card":
            self._get_memory_card()
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
            if not isinstance(slot, int) or not (0 <= slot <= 10):
                with _session_lock:
                    _session["save_in_progress"] = False
                self._send_json(400, {"error": "slot must be 0–10"})
                return
            # Slot 0 is a legacy value meaning "use the default autosave slot".
            if slot == 0:
                slot = SAVE_SLOT
            wait = body.get("wait", True)
            if wait:
                try:
                    ok = _save_and_exit(slot)
                finally:
                    with _session_lock:
                        _session["save_in_progress"] = False
                if not ok:
                    log.warning("streaming: save state failed (slot %d) — relaunching dashboard anyway", slot)
                self._send_json(200, {"status": "ok", "saved": ok, "slot": slot})
                # Relaunch to dashboard regardless of save result — PCSX2 is already dead.
                Thread(target=_launch_pcsx2, args=(None,), daemon=True).start()
            else:
                # Clear visible session state synchronously so that callers
                # polling /status immediately after this response observe
                # "no game running" instead of stale rom_path. The background
                # thread still runs the actual save+kill+relaunch.
                with _session_lock:
                    _session["rom_path"] = None
                    _session["rom_name"] = "Dashboard"
                    _session["started_at"] = None

                def _bg(s):
                    try:
                        ok = _save_and_exit(s)
                    finally:
                        with _session_lock:
                            _session["save_in_progress"] = False
                    if not ok:
                        log.warning("streaming: save state failed (slot %d) — relaunching dashboard anyway", s)
                    # Relaunch to dashboard regardless of save result — PCSX2 is already dead.
                    _launch_pcsx2(None)
                Thread(target=_bg, args=(slot,), daemon=True).start()
                self._send_json(200, {"status": "queued", "slot": slot})
            return

        if self.path == "/volume":
            body = self._read_body()
            level = body.get("level")
            if not isinstance(level, int) or not (0 <= level <= 100):
                self._send_json(400, {"error": "level must be an integer 0–100"})
                return
            result = _pactl("set-sink-volume", "@DEFAULT_SINK@", f"{level}%")
            if result.returncode != 0:
                self._send_json(500, {"error": "pactl failed", "detail": result.stderr.strip()})
                return
            log.info("Volume set to %d%%", level)
            self._send_json(200, {"status": "ok", "level": level})
            return

        if self.path == "/mute":
            body = self._read_body()
            if "mute" in body:
                mute_arg = "1" if body["mute"] else "0"
            else:
                mute_arg = "toggle"
            result = _pactl("set-sink-mute", "@DEFAULT_SINK@", mute_arg)
            if result.returncode != 0:
                self._send_json(500, {"error": "pactl failed", "detail": result.stderr.strip()})
                return
            mute_state = _pactl_get_mute()
            log.info("Mute %s", "on" if mute_state else "off")
            self._send_json(200, {"status": "ok", "mute": mute_state})
            return

        if self.path == "/save-state":
            with _session_lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
                if _session["save_in_progress"]:
                    self._send_json(409, {"error": "save already in progress"})
                    return
                _session["save_in_progress"] = True
            body = self._read_body()
            slot = body.get("slot", 1)
            if not isinstance(slot, int) or not (1 <= slot <= 10):
                with _session_lock:
                    _session["save_in_progress"] = False
                self._send_json(400, {"error": "slot must be 1–10"})
                return
            def _bg_save(s):
                try:
                    ok = _save_state(s)
                finally:
                    with _session_lock:
                        _session["save_in_progress"] = False
                if not ok:
                    log.warning("save-state: write not confirmed for slot %d", s)
            Thread(target=_bg_save, args=(slot,), daemon=True).start()
            self._send_json(200, {"status": "saving", "slot": slot})
            return

        if self.path == "/load-state":
            with _session_lock:
                if _session["rom_path"] is None:
                    self._send_json(409, {"error": "no game is running"})
                    return
            body = self._read_body()
            slot = body.get("slot", 1)
            if not isinstance(slot, int) or not (1 <= slot <= 10):
                self._send_json(400, {"error": "slot must be 1–10"})
                return
            ok = _load_state(slot)
            if ok:
                self._send_json(200, {"status": "ok", "loaded": True, "slot": slot})
            else:
                # 503: PCSX2 is the upstream and we couldn't reach it over
                # PINE or xdotool. The broker itself is healthy.
                self._send_json(503, {
                    "status": "error",
                    "loaded": False,
                    "slot": slot,
                    "error": "could not deliver load-state to PCSX2 (PINE and xdotool both failed)",
                })
            return

        if self.path != "/launch":
            self._send_json(404, {"error": "not found"})
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

        # Resume-from-state: load this slot once the game VM is up.
        load_slot = body.get("load_slot")
        if load_slot is not None and (
            not isinstance(load_slot, int) or not (1 <= load_slot <= 10)
        ):
            self._send_json(400, {"error": "load_slot must be 1–10"})
            return

        # Check save_in_progress and claim launch_in_progress in the same lock
        # acquisition — checking them separately lets a save start in the gap,
        # and the launch would then kill PCSX2 mid-savestate.
        with _session_lock:
            if _session["save_in_progress"]:
                self._send_json(409, {"error": "save in progress"})
                return
            if _session["launch_in_progress"]:
                self._send_json(409, {"error": "launch already in progress"})
                return
            _session["launch_in_progress"] = True

        Thread(
            target=_launch_pcsx2, args=(str(rom_path), True), daemon=True
        ).start()
        if load_slot is not None:
            Thread(
                target=_deferred_load_state, args=(load_slot,), daemon=True
            ).start()
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


# ── Main ──────────────────────────────────────────────────────────────────────

def _graceful_shutdown(server: HTTPServer, signum: int) -> None:
    """Stop the HTTP listener, finish any in-flight save, then kill PCSX2.
    Triggered on SIGTERM/SIGINT so `systemctl stop` doesn't drop a save mid-write."""
    log.info("Received signal %d — beginning graceful shutdown", signum)
    # Stop accepting new requests immediately so /save-and-exit can't race us.
    Thread(target=server.shutdown, daemon=True).start()

    # Wait briefly for any in-flight save to finish. The /save-and-exit handler
    # holds save_in_progress for the duration of the save request + write poll.
    deadline = time.monotonic() + max(PINE_WAIT, 5.0)
    while time.monotonic() < deadline:
        with _session_lock:
            if not _session["save_in_progress"]:
                break
        time.sleep(0.2)
    else:
        log.warning("Shutdown: in-flight save did not complete within %.1fs — killing PCSX2 anyway", PINE_WAIT)

    _kill_pcsx2()
    log.info("Shutdown complete")


def main():
    log.info("Broker starting — waiting for desktop X display...")
    display = _wait_for_x_display(timeout=30.0)
    if display is None:
        log.error("No live X display appeared within 30s; pcsx2-qt will likely fail to launch")
    else:
        # Update ENV in case the display we found is different from the one
        # we detected at module load time (Xvfb may not be up yet then).
        ENV["DISPLAY"] = display
        _XDOTOOL_ENV["DISPLAY"] = display
        log.info("Desktop ready on DISPLAY=%s", display)

    # Safety net for stale processes on hot-reload; init-pcsx2-config already
    # disables the labwc autostart so this should normally find nothing.
    # -x: match the binary name exactly. -f matches the entire command line and
    # would also kill processes that merely *mention* "pcsx2-qt" (editors, shells,
    # log tailers, etc.).
    result = subprocess.run(["pkill", "-9", "-x", "pcsx2-qt"], capture_output=True)
    if result.returncode == 0:
        log.info("Killed stale pcsx2-qt instance(s) on startup.")
        if not _wait_for_no_pcsx2(timeout=3.0):
            log.warning("Stale pcsx2-qt instance still running after SIGKILL — OS may be slow")

    _patch_ini()
    _launch_pcsx2_internal(None)

    # ThreadingHTTPServer: /save-and-exit with wait=true blocks its handler for
    # up to PINE_WAIT seconds; a single-threaded server would stall /health and
    # /status for the duration. Session state is already lock-protected.
    server = ThreadingHTTPServer(("0.0.0.0", PORT), BrokerHandler)
    log.info("ROM broker listening on port %d", PORT)
    if SECRET:
        log.info("Shared secret auth enabled")
    else:
        log.warning("BROKER_SECRET not set — all POST/DELETE endpoints are unauthenticated")

    # Install a single handler for both SIGTERM (systemd stop) and SIGINT
    # (Ctrl-C). We intentionally don't use server.serve_forever()'s default
    # KeyboardInterrupt path because it doesn't cover SIGTERM.
    def _handle(signum, _frame):
        _graceful_shutdown(server, signum)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
