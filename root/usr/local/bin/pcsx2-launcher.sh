#!/bin/bash
export DISPLAY=:0
export HOME=/config
export USER=abc
export QT_QPA_PLATFORM=xcb

ROM_FILE="/tmp/pcsx2-rom"
INI="/config/.config/PCSX2/inis/PCSX2.ini"

# Re-patch PINE each launch since PCSX2 resets it on exit
if [ -f "$INI" ]; then
    sed -i 's/EnablePINE = false/EnablePINE = true/' "$INI"
fi

if [ -f "$ROM_FILE" ]; then
    ROM_PATH=$(cat "$ROM_FILE")
    exec pcsx2-qt -batch -fullscreen "$ROM_PATH"
else
    exec pcsx2-qt
fi
