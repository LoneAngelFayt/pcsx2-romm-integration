#!/bin/bash
export DISPLAY=:0
export HOME=/config
export USER=abc
export QT_QPA_PLATFORM=xcb

ROM_FILE="/tmp/pcsx2-rom"

if [ -f "$ROM_FILE" ]; then
    ROM_PATH=$(cat "$ROM_FILE")
    exec pcsx2-qt -batch -fullscreen "$ROM_PATH"
else
    exec pcsx2-qt
fi
