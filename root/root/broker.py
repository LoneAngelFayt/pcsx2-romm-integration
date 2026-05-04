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

# PCSX2 2.x creates the PINE socket as pcsx2.sock (not pcsx2-{slot})
PINE_SOCKET  = Path(os.environ.get("PINE_SOCKET", str(Path(ENV["XDG_RUNTIME_DIR"]) / "pcsx2.sock")))
PINE_TIMEOUT = float(os.environ.get("PINE_TIMEOUT", "2.0"))   # connect + send timeout
PINE_WAIT    = float(os.environ.get("PINE_WAIT",   "20.0"))   # max seconds to poll for write completion
SAVE_SLOT    = int(os.environ.get("SAVE_SLOT", "10"))
SSTATE_DIR   = Path(os.environ.get("SSTATE_DIR", "/config/.config/PCSX2/sstates"))

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

# ── Session state ─────────────────────────────────────────────────────────────

_session_lock = Lock()
_session: dict = {
    "process":          None,
    "rom_path":         None,
    "rom_name":         None,
    "started_at":       None,
    "is_managed":       False,
    "save_in_progress": False,
    "current_slot":     1,      # tracks PCSX2's active save state slot (resets to 1 on each launch)
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

    log.info("Launching PCSX2 (rom=%s)", rom_path or "dashboard")
    log.debug("Launching: %s", " ".join(cmd))

    # Open the pcsx2-qt log in append mode so we keep history across launches.
    # Failure to open it is non-fatal: emulator still launches, just without
    # captured output. We try to keep the file under abc:abc so pcsx2-qt can
    # write to it after sudo drops privileges.
    try:
        log_fh = open(PCSX2_LOG_PATH, "ab", buffering=0)
        try:
            os.chown(PCSX2_LOG_PATH, 1000, 1000)  # abc uid/gid in linuxserver images
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


def _launch_pcsx2(rom_path):
    _kill_pcsx2()
    _drain_gamepad_sockets()
    _patch_ini()
    if not _wait_for_no_pcsx2():
        log.warning("pcsx2-qt still running after kill+drain; relaunching anyway")
    with _session_lock:
        _session["rom_path"] = rom_path
        _session["rom_name"] = Path(rom_path).stem if rom_path else "Dashboard"
        # Only stamp a session start when an actual ROM is being played. The
        # dashboard is an idle state.
        _session["started_at"] = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) if rom_path else None
        )
    _launch_pcsx2_internal(rom_path)


def _find_pine_socket() -> Path | None:
    """Locate PCSX2's PINE Unix socket. Tries the configured path first, then
    falls back to a glob search so the correct name is discovered automatically."""
    if PINE_SOCKET.exists():
        return PINE_SOCKET
    runtime_dir = Path(ENV["XDG_RUNTIME_DIR"])
    for pattern in ("pcsx2-*", "pcsx2.sock"):
        found = sorted(runtime_dir.glob(pattern))
        if found:
            log.debug("PINE: configured path %s absent — using %s", PINE_SOCKET, found[0])
            return found[0]
    for pattern in ("pcsx2-*", "pcsx2.sock"):
        found = sorted(Path("/tmp").glob(pattern))
        if found:
            log.debug("PINE: found socket in /tmp at %s", found[0])
            return found[0]
    log.error("PINE: no socket found (looked in %s and /tmp)", runtime_dir)
    return None


def _sstate_snapshot() -> dict:
    """Return {Path: (size, mtime)} for every .p2s file currently in SSTATE_DIR."""
    if not SSTATE_DIR.is_dir():
        log.debug("PINE: SSTATE_DIR absent — %s", SSTATE_DIR)
        return {}
    snap = {}
    for p in SSTATE_DIR.glob("*.p2s"):
        try:
            st = p.stat()
            snap[p] = (st.st_size, st.st_mtime)
        except OSError:
            pass
    log.debug("PINE: snapshot — %d file(s) in %s", len(snap), SSTATE_DIR)
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
                    log.debug("PINE: write detected — %s (%d bytes, mtime %.3f)", p.name, size, mtime)
                    break
                else:
                    log.debug("PINE: %s unchanged (mtime %.3f)", p.name, mtime)
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
                        "PINE: save state write complete — %s (%d bytes) in %.1fs",
                        target.name, last_size,
                        time.monotonic() - start,
                    )
                    return True

        time.sleep(POLL_SECS)

    return False


def _pine_save_state(slot: int) -> bool:
    """Send MsgSaveState (opcode 9) to PCSX2 via the PINE Unix socket.

    Wire format: [uint32 LE: payload length] [0x09] [slot byte]

    Confirmed behaviour in PCSX2 2.6.3: the command is received and the save
    state file IS written to SSTATE_DIR, but PCSX2 never sends a socket
    response for any PINE opcode. The write can take 10–20 s for large games.
    We close the socket immediately after sending and poll SSTATE_DIR for up
    to PINE_WAIT seconds (default 20 s), stopping as soon as the write
    stabilises.

    Returns True if the command was successfully sent.
    """
    socket_path = _find_pine_socket()
    if socket_path is None:
        return False

    before = _sstate_snapshot()

    # MsgSaveState opcode = 9 per PCSX2 PINE IPC spec (pcsx2/PINE.cpp)
    payload = bytes([9, slot & 0xFF])
    msg = struct.pack("<I", len(payload)) + payload
    try:
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(PINE_TIMEOUT)
            s.connect(str(socket_path))
            s.sendall(msg)
            # Keep the socket open while polling — PCSX2 only processes the
            # command (and writes the save file) while the connection is alive.
            # It attempts to write a response; closing early causes EPIPE and
            # aborts the save.
            log.info("PINE: save command sent (slot %d) — waiting for write (max %.1fs)", slot, PINE_WAIT)
            deadline = time.monotonic() + PINE_WAIT
            ok = _wait_for_sstate_write(before, deadline)
    except FileNotFoundError:
        log.error("PINE save state (slot %d): socket %s vanished between discovery and connect", slot, socket_path)
        return False
    except ConnectionRefusedError:
        log.error("PINE save state (slot %d): connection refused on %s — PCSX2 may have just exited", slot, socket_path)
        return False
    except _socket.timeout:
        log.error("PINE save state (slot %d): timed out (>%.1fs) connecting/sending to %s", slot, PINE_TIMEOUT, socket_path)
        return False
    except OSError as exc:
        log.error("PINE save state (slot %d): %s on %s (errno=%s)", slot, type(exc).__name__, socket_path, exc.errno)
        return False

    if not ok:
        log.warning("PINE: save state write not detected within %.1fs — proceeding anyway", PINE_WAIT)
    return True


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
    ok = _xdotool_save_state(slot)
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
    """Run pactl as abc so it connects to abc's PulseAudio instance."""
    return subprocess.run(
        _PACTL_CMD + ["pactl"] + list(args),
        capture_output=True, text=True, timeout=5,
    )


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
        return None
    return result.stdout.strip().endswith("yes")


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
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 64 * 1024)
        except ValueError:
            length = 0
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
            if not isinstance(slot, int) or not (1 <= slot <= 9):
                with _session_lock:
                    _session["save_in_progress"] = False
                self._send_json(400, {"error": "slot must be 1–9"})
                return
            def _bg_save(s):
                try:
                    ok = _xdotool_save_state(s)
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
            ok = _xdotool_load_state(slot)
            if ok:
                self._send_json(200, {"status": "ok", "loaded": True, "slot": slot})
            else:
                # 503: PCSX2 is the upstream and we couldn't reach its window
                # (xdotool find/dispatch failed). The broker itself is healthy.
                self._send_json(503, {
                    "status": "error",
                    "loaded": False,
                    "slot": slot,
                    "error": "could not deliver F3 to PCSX2 window (window not found or xdotool failure)",
                })
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

def _graceful_shutdown(server: HTTPServer, signum: int) -> None:
    """Stop the HTTP listener, finish any in-flight save, then kill PCSX2.
    Triggered on SIGTERM/SIGINT so `systemctl stop` doesn't drop a save mid-write."""
    log.info("Received signal %d — beginning graceful shutdown", signum)
    # Stop accepting new requests immediately so /save-and-exit can't race us.
    Thread(target=server.shutdown, daemon=True).start()

    # Wait briefly for any in-flight save to finish. The /save-and-exit handler
    # holds save_in_progress for the duration of the F1+poll cycle.
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

    server = HTTPServer(("0.0.0.0", PORT), BrokerHandler)
    log.info("ROM broker listening on port %d", PORT)
    if SECRET:
        log.info("Shared secret auth enabled")

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
