#!/bin/bash
# pcsx2-launcher.sh
# Called by labwc autostart instead of pcsx2-qt directly.
# If /tmp/pcsx2-rom exists and has content, launch that ROM.
# otherwise launch the dashboard normally.

ROM_FILE="/tmp/pcsx2-rom"

if [ -f "$ROM_FILE" ]; then
    ROM=$(cat "$ROM_FILE")
    rm -f "$ROM_FILE"
    if [ -n "$ROM" ]; then
        echo "[launcher] Starting ROM: $ROM"
        exec pcsx2-qt -batch -fullscreen -- "$ROM"
    fi
fi

echo "[launcher] Starting PCSX2 dashboard"
exec pcsx2-qt