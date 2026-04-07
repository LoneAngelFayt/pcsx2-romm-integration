#!/usr/bin/with-contenv bash

# 1. Disable the base-image labwc autostart before labwc reads it.
#    The broker manages pcsx2-qt directly; the autostart must not also launch it.
AUTOSTART="/config/.config/labwc/autostart"
mkdir -p "$(dirname "$AUTOSTART")"
printf '# Disabled by pcsx2-broker-mod\n' > "$AUTOSTART"
echo "[broker-mod] Disabled labwc autostart."

# 2. Patch the selkies input_handler.py keep-alive loop to also check reader.at_eof().
#    Without this patch, idle gamepad sockets (virtual slots 1-3) never detect client
#    disconnection: asyncio buffers EOF but eof_received() returns True for non-SSL
#    Unix sockets, so the transport stays open and writer.is_closing() never flips.
#    Adding reader.at_eof() causes the loop to exit as soon as the client half-closes.
INPUT_HANDLER="/lsiopy/lib/python3.12/site-packages/selkies/input_handler.py"
if [ -f "$INPUT_HANDLER" ]; then
    if grep -q "reader.at_eof()" "$INPUT_HANDLER"; then
        echo "[broker-mod] selkies input_handler.py already patched."
    else
        sed -i \
            's/while self\.running and not writer\.is_closing():/while self.running and not writer.is_closing() and not reader.at_eof():/' \
            "$INPUT_HANDLER"
        echo "[broker-mod] Patched selkies input_handler.py EOF detection."
    fi
else
    echo "[broker-mod] WARNING: selkies input_handler.py not found at $INPUT_HANDLER"
fi
