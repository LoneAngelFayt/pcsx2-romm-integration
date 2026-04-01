#!/bin/bash
# keeps pcsx2-launcher.sh alive for the life of the labwc session.
# launched as a background child of labwc's autostart mechanism.

PIDFILE=/tmp/pcsx2-autostart.pid

# kill any orphaned loop left over from a previous labwc session
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if [ "$OLD_PID" != "$$" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[pcsx2-autostart] Killing orphaned loop PID $OLD_PID"
        kill "$OLD_PID" 2>/dev/null
        pkill -P "$OLD_PID" 2>/dev/null
    fi
fi
echo $$ > "$PIDFILE"

export WAYLAND_DISPLAY=wayland-1
export XDG_RUNTIME_DIR=/config/.XDG

while true; do
    echo "[pcsx2-autostart] Starting launcher at $(date)"
    /usr/local/bin/pcsx2-launcher.sh
    EXIT=$?
    echo "[pcsx2-autostart] Launcher exited with code $EXIT, restarting in 1s"
    sleep 1
done
