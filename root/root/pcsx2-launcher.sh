#!/bin/bash
# pcsx2-launcher.sh
# Called by labwc autostart via foot.
# Loops forever — relaunches PCSX2 whenever it exits.

export WAYLAND_DISPLAY=wayland-1
export XDG_RUNTIME_DIR=/config/.XDG
export DISPLAY=:0
export HOME=/config
export USER=abc

ROM_FILE="/tmp/pcsx2-rom"

echo "[launcher] pcsx2-launcher.sh started"

# Wait for Xwayland to be ready
echo "[launcher] Waiting for Xwayland..."
for i in $(seq 1 30); do
    if [ -S "/tmp/.X11-unix/X0" ]; then
        echo "[launcher] Xwayland ready"
        break
    fi
    sleep 0.5
done

while true; do
    if [ -f "$ROM_FILE" ]; then
        ROM=$(cat "$ROM_FILE")
        rm -f "$ROM_FILE"
        if [ -n "$ROM" ]; then
            echo "[launcher] Starting ROM: $ROM"
            pcsx2-qt -batch -fullscreen -- "$ROM"
        else
            echo "[launcher] Starting PCSX2 dashboard"
            pcsx2-qt
        fi
    else
        echo "[launcher] Starting PCSX2 dashboard"
        pcsx2-qt
    fi

    echo "[launcher] PCSX2 exited, waiting 2 seconds before restart..."
    sleep 2
done