#!/bin/bash
export DISPLAY=:0
export HOME=/config
export USER=abc
export QT_QPA_PLATFORM=xcb

ROM_FILE="/tmp/pcsx2-rom"
INI="/config/.config/PCSX2/inis/PCSX2.ini"

# PCSX2 rewrites the INI on exit, reverting these — re-apply each launch
if [ -f "$INI" ]; then
    sed -i 's/EnablePINE = false/EnablePINE = true/' "$INI"
    sed -i 's|^Savestates = .*|Savestates = /config/.config/PCSX2/sstates|' "$INI"
    sed -i 's|^MemoryCards = .*|MemoryCards = /config/.config/PCSX2/memcards|' "$INI"
fi

if [ -f "$ROM_FILE" ]; then
    ROM_PATH=$(cat "$ROM_FILE")
    exec pcsx2-qt -batch -fullscreen "$ROM_PATH"
else
    exec pcsx2-qt
fi
