#!/usr/bin/with-contenv bash

INI="/config/.config/PCSX2/inis/PCSX2.ini"

# Wait for the ini file to exist (written on first PCSX2 launch)
echo "[broker-mod] Waiting for PCSX2.ini..."
for i in $(seq 1 30); do
    if [ -f "$INI" ]; then
        break
    fi
    sleep 1
done

if [ ! -f "$INI" ]; then
    echo "[broker-mod] PCSX2.ini not found, skipping config patch"
    exit 0
fi

echo "[broker-mod] Patching PCSX2.ini..."

# Enable PINE IPC
sed -i 's/EnablePINE=false/EnablePINE=true/' "$INI"

# Fix setup wizard blocking launch
# sed -i 's/SetupWizardIncomplete=true/SetupWizardIncomplete=false/' "$INI"

echo "[broker-mod] PCSX2.ini patched — PINE enabled on slot 28011"