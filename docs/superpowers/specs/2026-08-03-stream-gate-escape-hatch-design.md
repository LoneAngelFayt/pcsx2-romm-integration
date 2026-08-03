# Stream gate escape hatch (`STREAM_GATE`)

Date: 2026-08-03
Status: approved, ready for an implementation plan

## Problem

Commit `8e3777b` gated the browser-facing stream with an nginx `auth_request`
that calls the broker's `/verify`, which admits a request only when it carries
a session-bound stream token minted by `POST /launch`. The token is meant to
reach the browser because RomM appends it to the iframe host URL.

RomM does not do that yet. Upstream
[rommapp/romm#3211](https://github.com/rommapp/romm/pull/3211) ("feat: Emulator
streaming") is merged and is what people are running; its claim response
returns `"host": container.get("host", "")`, the operator's configured URL with
nothing appended. The RomM half of the gate lives in
[rommapp/romm#3856](https://github.com/rommapp/romm/pull/3856), which is still
open.

So on released RomM the claim does reach `POST /launch` and the broker does
mint a token, but the browser then loads the stream host bare. nginx sends the
subrequest to `/verify`, `_check_stream_token("")` answers "no stream token in
the request", and every request is refused: the document, every asset, and the
WebSocket upgrade alike. Anyone pulling `:latest` of the mod gets a permanently
black stream and no indication of why.

This is a hard lockout, and it is live right now for every user of the
published image.

## Decision

Add an operator switch that turns gate *enforcement* off, defaulted to off
until #3856 merges, then flip the default back.

The gate itself is kept. It is turned off, not removed, so that turning it back
on is a one-line config change rather than a revert.

## Design

### The knob

`STREAM_GATE`, values `off` and `token`, default `off`.

Parsed once at module load, beside the other stream constants near
`STREAM_TOKEN_GRACE` in `root/root/broker.py`.

An unrecognized value logs a warning and resolves to `off`, whatever the
default happens to be at the time. That is not simply "fall back to the
default": a typo that silently locks out the entire stream is the exact failure
this work exists to remove, so the fallback is pinned to the permissive side
rather than tracking the default. This mirrors the existing
`BROKER_INITIAL_SLOT` handling, which warns and falls back rather than raising.

### Enforcement is decided in the broker, not in nginx

`_verify_stream_decision` returns `(200, None, None)` immediately when the gate
is off, before any token extraction.

The nginx injection in
`root/etc/s6-overlay/s6-rc.d/init-pcsx2-config/init.sh` is left exactly as it
is, still gating every `server` block.

This placement is the point of the design. As `1736c84` recorded, the nginx
patch is written into the container's writable layer and is guarded by a grep
for its own marker, so a gate decided in the nginx config would need a
`--force-recreate` to switch off and a second one to switch back on. Decided in
the broker, switching is `docker compose up -d pcsx2` after editing the one
variable.

Not `docker compose restart`. Compose bakes the environment in at create time,
so a restart replays the old value and the gate silently stays off. The changed
variable is enough on its own to recreate the container, and because the init
guards its nginx patch behind a marker grep, re-patching the fresh layer costs
nothing.

### The token machinery is untouched

`POST /launch` still mints a token, `GET /status` still reports it, release
still clears it, and the TTL and grace window still tick. Only enforcement is
bypassed.

Two payoffs. When #3856 merges, the migration is setting `STREAM_GATE=token`
and bringing the container back up, with no other change. And an operator
running the #3856 branch today can turn the gate on immediately.

### Visibility

A startup log line in both directions, alongside the existing warning for an
unset `BROKER_SECRET`:

* `off`: `WARNING`, naming the consequence (the interactive desktop and the
  `/files` view of `/config/Desktop` are reachable by anyone who can reach
  3000 or 3001) and naming `STREAM_GATE=token` as the way to close it.
* `token`: `INFO`, so "is the gate actually on" is answerable from
  `docker logs pcsx2` without a shell in the container.

`GET /status` gains `"stream_gate": "off"` or `"stream_gate": "token"`. That
endpoint is behind `BROKER_SECRET`, and it makes the live mode diagnosable
remotely.

### Documentation

The README's Security section currently states as settled fact that the stream
token guards 3000/3001. That becomes wrong the moment this ships, so it is
rewritten to say the gate exists but ships off, why it ships off (upstream RomM
carries no token yet, with both PR links), and how to turn it on. The
environment variable table gains `STREAM_GATE`, and the `docker-compose`
example is updated to match.

The `# Gate the browser-facing stream` comment block in `init.sh` likewise
asserts unconditional enforcement. It gains a line stating that enforcement is
conditional on the broker's `STREAM_GATE`.

### Tests

`tests/test_broker.py` already calls `_verify_stream_decision` directly and
already reads module constants such as `broker.STREAM_TOKEN_TTL`, so the mode
can be pinned per test with `patch.object(broker, "STREAM_GATE", ...)`.

New coverage:

* `off` admits a request carrying no token at all.
* `off` admits a request carrying a garbage `stream_sid` cookie.
* `off` admits when no stream session is open.
* An unrecognized `STREAM_GATE` value resolves to `off`.
* `GET /status` reports the active mode.

The existing gate tests are re-pinned to an explicit `token` mode, so they keep
exercising the strict path instead of passing vacuously under the new default.

## Out of scope

Deliberately not built:

* No claim window or trust-on-first-use admission.
* No auto-detection of the RomM version or of whether a token ever arrives.
* No partial or per-vhost gating.

A switch that does one legible thing is the one that is safe to flip back.

## Accepted risk

With this shipped and defaulted off, `:latest` serves an interactive desktop
with the ROM library mounted, which is the state before `8e3777b`. That is
accepted for now as the lesser harm against locking out every user, and the
README states it plainly rather than burying it.

## Reverting to a gated default

When #3856 merges: change the default to `token`, update the README's Security
section and environment table, and note in the release that operators on an
older RomM must set `STREAM_GATE=off` to keep streaming.
