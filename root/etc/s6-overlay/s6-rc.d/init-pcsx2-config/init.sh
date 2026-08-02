#!/usr/bin/with-contenv bash

# Clean up stale Wayland and X11 sockets to ensure the compositor starts on the
# default display indices (wayland-0, :0) even if the container was killed
# forcefully. Persistence of these lock files on the host-mapped /config
# folder causes them to increment (wayland-1, :1, etc.) on relaunch, which
# breaks the hardcoded display expectations of the broker and stream.
XDG_RUNTIME_DIR="/config/.XDG"
mkdir -p "$XDG_RUNTIME_DIR"
# Only clean when no display server is alive: if s6 ordering ever changes
# and the compositor is already up, deleting its live socket kills the stream.
if pgrep -x Xvfb >/dev/null || pgrep -x Xwayland >/dev/null || pgrep -x labwc >/dev/null; then
    echo "[broker-mod] Display server already running, skipping socket cleanup."
else
    find "$XDG_RUNTIME_DIR" -name "wayland-*" -delete
    rm -rf /tmp/.X11-unix/X* /tmp/.X*lock
    echo "[broker-mod] Cleaned up stale display sockets."
fi

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

# Lock down the sudoers rule so sudo accepts it (requires mode 0440).
chmod 0440 /etc/sudoers.d/broker
echo "[broker-mod] sudoers rule set."

# Disable the labwc autostart so pcsx2-qt isn't launched a second time by the
# desktop session: the broker manages the process lifecycle directly.
AUTOSTART="/config/.config/labwc/autostart"
mkdir -p "$(dirname "$AUTOSTART")"
printf '# Disabled by pcsx2-broker-mod\n' > "$AUTOSTART"
echo "[broker-mod] Disabled labwc autostart."

# Patch the selkies input_handler.py keep-alive loop to check reader.at_eof().
# Without this, idle gamepad sockets never detect client disconnection because
# asyncio buffers the EOF but writer.is_closing() never flips on Unix sockets.
# Search the known install roots rather than assuming one: a base image that
# moves selkies out of /lsiopy, or bumps e.g. python3.12 → python3.13, must not
# silently cost us the patch. The find is scoped to these roots, not /, so it
# stays cheap at container start.
INPUT_HANDLER=""
for _root in /lsiopy /usr/lib /usr/local/lib /opt /config/.local; do
    [ -d "$_root" ] || continue
    INPUT_HANDLER=$(find "$_root" -path "*/selkies/input_handler.py" -type f 2>/dev/null | head -1)
    [ -n "$INPUT_HANDLER" ] && break
done
if [ -n "$INPUT_HANDLER" ]; then
    echo "[broker-mod] selkies input_handler.py found at $INPUT_HANDLER"
    if grep -q "reader.at_eof()" "$INPUT_HANDLER"; then
        echo "[broker-mod] selkies input_handler.py EOF patch already applied."
    else
        sed -i \
            's/while self\.running and not writer\.is_closing():/while self.running and not writer.is_closing() and not reader.at_eof():/' \
            "$INPUT_HANDLER"
        # Verify the substitution actually took: sed exits 0 even when the
        # pattern never matched, so grep is the only honest success signal.
        if grep -q "reader.at_eof()" "$INPUT_HANDLER"; then
            echo "[broker-mod] Patched selkies input_handler.py EOF detection."
        else
            echo "[broker-mod] ERROR: input_handler.py EOF pattern not found, patch NOT applied (upstream may have changed)"
        fi
    fi

    # Silence the selkies_gamepad logger: it emits ~80 INFO lines per launch cycle
    # (handler started/finished, config sent, arch specifier, active list changes ×8
    # sockets), which buries the broker's own output. Demote to WARNING to keep
    # errors and warnings while clearing the spam.
    #
    # Appended to the end of the module instead of sed-ing the assignment line.
    # An anchor can stop matching when upstream renames its variable, and that
    # failure is silent; appending always applies. Setting the level by logger
    # name reaches the same singleton whatever upstream calls its local. This
    # holds because selkies only ever calls basicConfig(level=INFO), which sets
    # the root logger and does not touch a child's explicit level, and nothing
    # upstream re-sets this one.
    GAMEPAD_MARKER="pcsx2-broker-mod:gamepad-loglevel"
    if grep -q "$GAMEPAD_MARKER" "$INPUT_HANDLER"; then
        echo "[broker-mod] selkies_gamepad log-level patch already applied."
    else
        cat >> "$INPUT_HANDLER" <<'PYEOF'

# pcsx2-broker-mod:gamepad-loglevel
# Demote the gamepad chatter so the broker's log stays readable. See the
# pcsx2-romm-integration init script for why this is appended, not patched.
import logging as _bm_logging
_bm_logging.getLogger("selkies_gamepad").setLevel(_bm_logging.WARNING)
PYEOF
        # Confirm both that the marker landed and that the file still parses:
        # a half-written append would take selkies down with it, which is a far
        # worse outcome than noisy logs.
        if ! grep -q "$GAMEPAD_MARKER" "$INPUT_HANDLER"; then
            echo "[broker-mod] ERROR: could not append the selkies_gamepad log-level patch to $INPUT_HANDLER (read-only filesystem?), gamepad INFO spam will remain"
        elif ! command -v python3 >/dev/null 2>&1 \
             || python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$INPUT_HANDLER" 2>/dev/null; then
            echo "[broker-mod] Patched selkies_gamepad log level to WARNING."
        else
            echo "[broker-mod] ERROR: input_handler.py no longer parses after the log-level patch, reverting"
            sed -i "/# $GAMEPAD_MARKER/,\$d" "$INPUT_HANDLER"
        fi
    fi
else
    echo "[broker-mod] ERROR: selkies input_handler.py not found under /lsiopy, /usr/lib, /usr/local/lib, /opt or /config/.local, so neither the gamepad EOF fix nor the log-level patch was applied"
fi

# Gate the browser-facing stream with nginx auth_request. The 3001 SSL vhost is
# the host RomM loads in the iframe; without this, anyone who learns the address
# gets an interactive desktop with the ROM library mounted, since RomM's auth
# never sits on this socket. auth_request sends every 3001 request to the
# broker's /verify, which checks the session-bound stream token RomM appends to
# the iframe URL and, on the first (query-token) hit, hands back a stream_sid
# cookie that carries every later asset and the WebSocket upgrade. Anchored on
# ssl_certificate_key, which appears only in the 3001 server block, so the plain
# 3000 vhost is untouched. The broker exempts /verify from its shared secret
# because nginx cannot forward that secret and the stream token is the credential.
NGINX_SITE="/etc/nginx/sites-available/default"
if [ -f "$NGINX_SITE" ]; then
    if grep -q "RomM stream gate" "$NGINX_SITE"; then
        echo "[broker-mod] nginx stream gate already applied."
    else
        awk '
          { print }
          /ssl_certificate_key/ && !injected {
            print "  # ── RomM stream gate (pcsx2-broker-mod) ──"
            print "  auth_request /_stream_auth;"
            print "  auth_request_set $stream_set_cookie $upstream_http_set_cookie;"
            print "  add_header Set-Cookie $stream_set_cookie;"
            print "  location = /_stream_auth {"
            print "    internal;"
            print "    auth_request off;"
            print "    proxy_pass http://127.0.0.1:8000/verify;"
            print "    proxy_pass_request_body off;"
            print "    proxy_set_header Content-Length \"\";"
            print "    proxy_set_header X-Original-URI $request_uri;"
            print "  }"
            injected = 1
          }
        ' "$NGINX_SITE" > "$NGINX_SITE.tmp" && mv "$NGINX_SITE.tmp" "$NGINX_SITE"
        if grep -q "RomM stream gate" "$NGINX_SITE"; then
            echo "[broker-mod] Applied nginx stream gate to 3001 vhost."
            if ! nginx -t 2>/dev/null; then
                echo "[broker-mod] WARNING: nginx -t failed now (ssl cert may not exist yet at init); nginx revalidates at start."
            fi
        else
            rm -f "$NGINX_SITE.tmp"
            echo "[broker-mod] ERROR: nginx stream gate not applied (ssl_certificate_key anchor missing; base image may have changed)."
        fi
    fi
else
    echo "[broker-mod] WARNING: nginx site config not found at $NGINX_SITE"
fi
