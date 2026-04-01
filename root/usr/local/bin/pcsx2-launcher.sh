#!/bin/bash
export DISPLAY=:0
export HOME=/config
export USER=abc
export QT_QPA_PLATFORM=xcb

ROM_FILE="/tmp/pcsx2-rom"
INI="/config/.config/PCSX2/inis/PCSX2.ini"

echo "[pcsx2-launcher] ── launch start $(date) ──"

# ── INI patches ───────────────────────────────────────────────────────────────
# PCSX2 rewrites the INI on exit, reverting these — re-apply each launch.
if [ -f "$INI" ]; then
    echo "[pcsx2-launcher] INI found: $INI"

    if grep -q "EnablePINE = false" "$INI"; then
        sed -i 's/EnablePINE = false/EnablePINE = true/' "$INI"
        echo "[pcsx2-launcher] INI patch: EnablePINE false→true"
    else
        echo "[pcsx2-launcher] INI: EnablePINE=$(grep -o 'EnablePINE = [^ ]*' "$INI" || echo '(key absent)')"
    fi

    # -fullscreen flag enables Big Picture Mode (game list), not game fullscreen.
    # Pin StartFullscreen in the ini so the game renders fullscreen when booted.
    sed -i 's/^StartFullscreen = .*/StartFullscreen = true/' "$INI"

    if grep -q "^Savestates = " "$INI"; then
        sed -i 's|^Savestates = .*|Savestates = /config/.config/PCSX2/sstates|' "$INI"
        echo "[pcsx2-launcher] INI patch: Savestates path pinned"
    else
        echo "[pcsx2-launcher] WARNING: Savestates key absent from INI — path not pinned"
    fi

    if grep -q "^MemoryCards = " "$INI"; then
        sed -i 's|^MemoryCards = .*|MemoryCards = /config/.config/PCSX2/memcards|' "$INI"
        echo "[pcsx2-launcher] INI patch: MemoryCards path pinned"
    else
        echo "[pcsx2-launcher] WARNING: MemoryCards key absent from INI — path not pinned"
    fi
else
    echo "[pcsx2-launcher] WARNING: INI not found at $INI — patches skipped (first run?)"
fi

# ── BIOS check ────────────────────────────────────────────────────────────────
BIOS_DIR="/config/bios"
if [ -d "$BIOS_DIR" ]; then
    BIOS_FILES=$(find "$BIOS_DIR" -maxdepth 1 -type f | wc -l)
    echo "[pcsx2-launcher] BIOS dir exists, $BIOS_FILES file(s): $(ls "$BIOS_DIR" | tr '\n' ' ')"
    if [ "$BIOS_FILES" -eq 0 ]; then
        echo "[pcsx2-launcher] WARNING: BIOS dir is empty — PCSX2 will exit immediately in batch mode"
    fi
else
    echo "[pcsx2-launcher] WARNING: BIOS dir missing at $BIOS_DIR — PCSX2 will exit immediately"
fi

# ── Xwayland check ────────────────────────────────────────────────────────────
X_SOCK="/tmp/.X11-unix/X0"
if [ -S "$X_SOCK" ]; then
    echo "[pcsx2-launcher] Xwayland socket $X_SOCK present"
else
    echo "[pcsx2-launcher] WARNING: Xwayland socket $X_SOCK missing — PCSX2 cannot connect to display"
fi

# ── ROM launch or dashboard ───────────────────────────────────────────────────
if [ -f "$ROM_FILE" ]; then
    ROM_PATH=$(cat "$ROM_FILE")
    echo "[pcsx2-launcher] ROM signal found, path: '$ROM_PATH'"
    if [ ! -f "$ROM_PATH" ]; then
        echo "[pcsx2-launcher] WARNING: ROM does not exist at '$ROM_PATH' — launching dashboard instead"
        rm -f "$ROM_FILE"
        exec pcsx2-qt
    fi
    rm -f "$ROM_FILE"
    echo "[pcsx2-launcher] Exec: pcsx2-qt -batch '$ROM_PATH'"
    pcsx2-qt -batch "$ROM_PATH"
    EXIT_CODE=$?
    echo "[pcsx2-launcher] pcsx2-qt exited with code $EXIT_CODE"
    exit $EXIT_CODE
else
    echo "[pcsx2-launcher] No ROM signal — launching dashboard"
    exec pcsx2-qt
fi
