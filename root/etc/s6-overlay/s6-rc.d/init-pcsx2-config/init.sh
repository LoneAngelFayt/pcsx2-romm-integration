#!/usr/bin/with-contenv bash

# Clean up stale Wayland and X11 sockets to ensure the compositor starts on the 
# default display indices (wayland-0, :0) even if the container was killed 
# forcefully.  Persistence of these lock files on the host-mapped /config 
# folder causes them to increment (wayland-1, :1, etc.) on relaunch, which 
# breaks the hardcoded display expectations of the broker and stream.
XDG_RUNTIME_DIR="/config/.XDG"
mkdir -p "$XDG_RUNTIME_DIR"
find "$XDG_RUNTIME_DIR" -name "wayland-*" -delete
rm -rf /tmp/.X11-unix/X* /tmp/.X*lock
echo "[broker-mod] Cleaned up stale display sockets."

# Ensure python3 is available for the broker service.
if ! command -v python3 &>/dev/null; then
    echo "[broker-mod] Installing python3..."
    apt-get update -qq && apt-get install -y -qq python3 \
        || echo "[broker-mod] ERROR: failed to install python3"
fi

# Lock down the sudoers rule so sudo accepts it (requires mode 0440).
chmod 0440 /etc/sudoers.d/broker
echo "[broker-mod] sudoers rule set."

# Disable the labwc autostart so pcsx2-qt isn't launched a second time by the
# desktop session — the broker manages the process lifecycle directly.
AUTOSTART="/config/.config/labwc/autostart"
mkdir -p "$(dirname "$AUTOSTART")"
printf '# Disabled by pcsx2-broker-mod\n' > "$AUTOSTART"
echo "[broker-mod] Disabled labwc autostart."

# Patch the selkies input_handler.py keep-alive loop to check reader.at_eof().
# Without this, idle gamepad sockets never detect client disconnection because
# asyncio buffers the EOF but writer.is_closing() never flips on Unix sockets.
INPUT_HANDLER="/lsiopy/lib/python3.12/site-packages/selkies/input_handler.py"
if [ -f "$INPUT_HANDLER" ]; then
    if grep -q "reader.at_eof()" "$INPUT_HANDLER"; then
        echo "[broker-mod] selkies input_handler.py EOF patch already applied."
    else
        sed -i \
            's/while self\.running and not writer\.is_closing():/while self.running and not writer.is_closing() and not reader.at_eof():/' \
            "$INPUT_HANDLER" \
            || echo "[broker-mod] ERROR: sed patch failed on input_handler.py"
        echo "[broker-mod] Patched selkies input_handler.py EOF detection."
    fi

    # Silence the selkies_gamepad logger — it emits ~80 INFO lines per launch cycle
    # (handler started/finished, config sent, arch specifier, active list changes ×8
    # sockets). Demote to WARNING to keep errors/warnings while clearing the spam.
    if grep -q "setLevel(logging.WARNING)" "$INPUT_HANDLER"; then
        echo "[broker-mod] selkies_gamepad log-level patch already applied."
    else
        if sed -i \
            's/logger_selkies_gamepad = logging.getLogger("selkies_gamepad")/logger_selkies_gamepad = logging.getLogger("selkies_gamepad")\nlogger_selkies_gamepad.setLevel(logging.WARNING)/' \
            "$INPUT_HANDLER"; then
            echo "[broker-mod] Patched selkies_gamepad log level to WARNING."
        else
            echo "[broker-mod] ERROR: sed patch failed setting selkies_gamepad log level"
        fi
    fi
else
    echo "[broker-mod] WARNING: selkies input_handler.py not found at $INPUT_HANDLER"
fi
