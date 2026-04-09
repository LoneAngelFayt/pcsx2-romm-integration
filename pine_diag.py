#!/usr/bin/env python3
"""
pine_diag.py — PINE IPC diagnostic for PCSX2.

Run this INSIDE the container while PCSX2 has a game loaded:
  docker exec -it pcsx2 python3 /root/pine_diag.py

It will:
  1. Locate the PINE socket
  2. Send MsgVersion (opcode 8) — verifies basic IPC
  3. Send MsgStatus  (opcode 15) — shows emulator state
  4. Send MsgSaveState (opcode 9, slot 0) — attempts save, waits 30s for response
  5. Check known save state directories for new files
"""

import glob
import os
import socket
import struct
import sys
import time

XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/config/.XDG")

# ── socket discovery ──────────────────────────────────────────────────────────

def find_socket():
    for path in [
        os.path.join(XDG_RUNTIME_DIR, "pcsx2.sock"),
        "/tmp/pcsx2.sock",
    ]:
        if os.path.exists(path):
            return path
    for pattern in (
        os.path.join(XDG_RUNTIME_DIR, "pcsx2-*"),
        os.path.join(XDG_RUNTIME_DIR, "pcsx2*"),
        "/tmp/pcsx2-*",
        "/tmp/pcsx2*",
    ):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


# ── low-level send / recv ─────────────────────────────────────────────────────

def recv_exact(s, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"Socket closed after {len(buf)}/{n} bytes")
        buf.extend(chunk)
    return bytes(buf)


def pine_call(sock_path, opcode, args=b"", timeout=10.0, shut_wr=False):
    """
    Send one PINE command and return the raw response bytes (excluding the
    4-byte length prefix), or None on error.

    Tries two payload-size encodings:
      format A: length = len(opcode_byte + args)          [pine-python style]
      format B: length = len(opcode_byte + args) + 4      [some builds add header]
    """
    payload = bytes([opcode]) + args
    results = {}

    for label, length_val in [
        ("A: len=opcode+args", len(payload)),
        ("B: len=opcode+args+4", len(payload) + 4),
        ("C: len=1 (cmd count)", 1),
    ]:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(sock_path)
                msg = struct.pack("<I", length_val) + payload
                print(f"  [{label}] sending: {msg.hex(' ')}")
                s.sendall(msg)
                if shut_wr:
                    s.shutdown(socket.SHUT_WR)
                # Try to read response
                resp_hdr = recv_exact(s, 4)
                resp_len = struct.unpack("<I", resp_hdr)[0]
                resp_data = recv_exact(s, resp_len) if resp_len > 0 else b""
                results[label] = resp_data
                print(f"  [{label}] response ({resp_len} bytes): {resp_data.hex(' ')}")
                return resp_data  # first format that works
        except Exception as exc:
            print(f"  [{label}] failed: {exc}")
            results[label] = None

    return None


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    sock_path = find_socket()
    if not sock_path:
        print("ERROR: PINE socket not found. Is PCSX2 running with EnablePINE=true?")
        sys.exit(1)
    print(f"Found PINE socket: {sock_path}\n")

    # --- MsgVersion (opcode 8, no args) ---
    print("=== MsgVersion (opcode 8) ===")
    ver = pine_call(sock_path, 8, timeout=5.0)
    if ver:
        print(f"  Version string: {ver!r}")
    print()

    # --- MsgStatus (opcode 15, no args) ---
    print("=== MsgStatus (opcode 15) ===")
    status = pine_call(sock_path, 15, timeout=5.0)
    if status and len(status) >= 1:
        codes = {0: "Running", 1: "Paused", 2: "Stopped", 3: "Shutdown"}
        code = status[0] if status[0] != 0xFF else 0xFF
        print(f"  Status code: 0x{code:02X} ({codes.get(code, 'unknown')})")
    print()

    # --- Snapshot save state directory before save ---
    sstate_dirs = [
        "/config/.local/share/PCSX2/sstates",
        "/config/.config/PCSX2/sstates",
        "/config/Documents/PCSX2/sstates",
        os.path.expanduser("~/.local/share/PCSX2/sstates"),
        os.path.expanduser("~/.config/PCSX2/sstates"),
    ]
    before = {}
    for d in sstate_dirs:
        if os.path.isdir(d):
            before[d] = set(os.listdir(d))
            print(f"Save state dir found: {d}  ({len(before[d])} file(s) before)")
        else:
            print(f"Save state dir absent: {d}")
    print()

    # --- MsgSaveState (opcode 9, slot 0) — long timeout ---
    print("=== MsgSaveState (opcode 9, slot 0) — 30s timeout ===")
    t0 = time.monotonic()
    result = pine_call(sock_path, 9, args=bytes([0]), timeout=30.0)
    elapsed = time.monotonic() - t0
    if result is not None:
        ok = result[0] if result else 0xFF
        print(f"  Result: {'IPC_OK' if ok == 0 else 'IPC_FAIL'} (0x{ok:02X}) after {elapsed:.1f}s")
    else:
        print(f"  No response after {elapsed:.1f}s")

    # Give PCSX2 a moment to flush
    print("  Waiting 3s for disk write...")
    time.sleep(3)
    print()

    # --- Check for new save state files ---
    print("=== Save state files (after) ===")
    new_files = []
    for d in sstate_dirs:
        if os.path.isdir(d):
            after = set(os.listdir(d))
            added = after - before.get(d, set())
            if added:
                print(f"  NEW in {d}:")
                for f in sorted(added):
                    full = os.path.join(d, f)
                    size = os.path.getsize(full)
                    print(f"    {f}  ({size:,} bytes)")
                    new_files.append(full)
            else:
                print(f"  No new files in {d}")
        else:
            # Check if it appeared
            if os.path.isdir(d):
                print(f"  Directory created: {d}")
                for f in os.listdir(d):
                    print(f"    {f}")
    print()

    # --- Broader search for any .p2s files ---
    print("=== All .p2s files on filesystem ===")
    for root_dir in ["/config", "/root", "/tmp"]:
        for dirpath, _, filenames in os.walk(root_dir):
            for fn in filenames:
                if fn.endswith(".p2s"):
                    full = os.path.join(dirpath, fn)
                    size = os.path.getsize(full)
                    mtime = time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(full)))
                    print(f"  {full}  ({size:,} bytes, modified {mtime})")

    if not new_files:
        print("  None found — save state may not have been created.")


if __name__ == "__main__":
    main()
