#!/usr/bin/with-contenv bash

# Package and asset fetching, split out of init-pcsx2-config because it is slow
# and networked. init-pcsx2-config gates the whole service stack (nginx, xorg,
# selkies all wait on it), so anything that can spend a minute on apt-get or a
# GitHub download has to live somewhere that only the broker waits for.

# Ensure python3 is available for the broker service, and libshaderc for
# PCSX2's Vulkan renderer. The base image ships without it, so on a host
# with a GPU the GS device fails shader compilation and pcsx2-qt exits
# within a second of booting any game (the gameless dashboard never
# initializes GS, masking the breakage until a game is launched).
_pkgs=()
command -v python3 &>/dev/null || _pkgs+=(python3)
ldconfig -p | grep -q libshaderc.so.1 || _pkgs+=(libshaderc1)
if [ ${#_pkgs[@]} -gt 0 ]; then
    echo "[broker-mod] Installing: ${_pkgs[*]}"
    apt-get update -qq && apt-get install -y -qq "${_pkgs[@]}" \
        || echo "[broker-mod] ERROR: failed to install ${_pkgs[*]}"
fi

# Ubuntu's +dfsg PCSX2 package strips patches.zip from the resources dir,
# so every game boot warns "Built-in game patches are not available" and
# titles that rely on compatibility patches misbehave. Fetch the official
# archive from the PCSX2 project. Non-fatal if offline: games still run.
PATCHES_ZIP="/usr/share/PCSX2/resources/patches.zip"
if [ ! -s "$PATCHES_ZIP" ]; then
    echo "[broker-mod] Downloading PCSX2 patches.zip..."
    # Download to a temp path and verify it is a well-formed zip before
    # installing: the release URL floats ("latest"), so a checksum can't be
    # pinned, but a truncated or non-zip response must never land in place.
    # Bounded so an unresponsive GitHub can't stall container init for an
    # optional file.
    if curl -fsSL --connect-timeout 10 --max-time 60 -o "$PATCHES_ZIP.tmp" \
        "https://github.com/PCSX2/pcsx2_patches/releases/latest/download/patches.zip" \
        && python3 -c 'import sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    sys.exit(1 if z.testzip() is not None or not z.namelist() else 0)' \
            "$PATCHES_ZIP.tmp" 2>/dev/null; then
        chmod 644 "$PATCHES_ZIP.tmp"
        mv "$PATCHES_ZIP.tmp" "$PATCHES_ZIP"
        echo "[broker-mod] patches.zip installed."
    else
        rm -f "$PATCHES_ZIP.tmp"
        echo "[broker-mod] WARNING: patches.zip download failed or corrupt; built-in game patches unavailable."
    fi
fi
