# Stream Gate Escape Hatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `STREAM_GATE` operator switch, defaulted to `off`, that stops the nginx stream gate from locking out every user of released RomM.

**Architecture:** Enforcement becomes a broker-side decision rather than an nginx-side one. `_verify_stream_decision` short-circuits to a 200 when the gate is off; the nginx `auth_request` injection is left untouched and still gates every vhost, so switching modes is one environment variable instead of a `--force-recreate` in each direction. The token machinery (mint, report, clear, TTL, grace) keeps running unchanged, so turning the gate back on when rommapp/romm#3856 merges is one environment variable.

**Tech Stack:** Python 3, standard library only (no third party imports in `broker.py`, deliberately). Tests are `unittest`, not pytest. Lint is `ruff`. Shell is POSIX `sh` for the s6 init scripts.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-08-03-stream-gate-escape-hatch-design.md`. Read it before starting.
- **Stdlib only.** `broker.py` ships into the container as a single file with no dependency install. Do not add imports outside the standard library.
- **No em dashes or en dashes anywhere**, including code comments, log strings, test docstrings, commit messages and Markdown. This repo swept them out in `ee8f00a` and `7f0ab8f`; use a comma, colon, period or parentheses instead, and ASCII hyphens for numeric ranges (`1-10`, never the en dash form). Verify with:
  `grep -rnP '[\x{2010}-\x{2015}\x{2212}]' README.md root/ tests/ docs/`
- **No trailing whitespace** (`311b320`, `62d1a34`).
- **Every commit must be signed.** The repo has `commit.gpgsign=true` and the key signs without a prompt. Never pass `--no-gpg-sign`.
- **Every commit on this branch uses the `fix(<scope>):` subject.** Not `feat:`, not `docs:`, not `chore:`. `.releaserc.json` runs semantic-release off `main`, where `feat:` cuts a minor version and this branch is a bugfix for a shipped lockout, so it takes a patch. Scopes follow the existing log: `broker` for the Python service, `stream` for the gate and the stream path, `mod` for the s6 and image layer. Bodies explain the why, in the style of the existing log.
- **Run the full suite and the linter before every commit:**
  ```bash
  python3 -m unittest discover -s tests -q && ruff check .
  ```
  Both must be clean. The suite currently has 114 tests passing.
- **The mode values are exactly `off` and `token`.** Lowercase, compared after `.strip().lower()`.
- **Never change `root/etc/s6-overlay/s6-rc.d/init-pcsx2-config/init.sh` nginx behavior.** Only its comments change, in Task 4. The gate stays injected into every `server` block.

---

## File Structure

Four existing files change. No new files.

| File | Responsibility | Tasks |
|---|---|---|
| `root/root/broker.py` | Resolve the mode, bypass enforcement, report the mode on `/status` and at startup | 1, 2, 3 |
| `tests/test_broker.py` | Cover mode parsing, off-mode admission, re-pin the existing gate tests to `token`, cover the reporting | 1, 2, 3 |
| `README.md` | Environment table row, Security section rewrite, compose example | 4 |
| `root/etc/s6-overlay/s6-rc.d/init-pcsx2-config/init.sh` | Comment correction only, no behavior change | 4 |

---

### Task 1: Resolve the `STREAM_GATE` mode

Introduces the constant and its parsing. Nothing enforces it yet, so this task changes no behavior and cannot break the existing gate tests. Task 2 wires it in.

**Files:**
- Modify: `root/root/broker.py` (insert after the `log = logging.getLogger("broker")` line, currently line 194)
- Test: `tests/test_broker.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `STREAM_GATE_MODES: tuple[str, ...]`, the value `("off", "token")`
  - `STREAM_GATE_DEFAULT: str`, the value `"off"`
  - `_resolve_stream_gate(raw: str) -> str`, returns `"off"` or `"token"`
  - `STREAM_GATE: str`, the resolved module-level mode, read at import from the environment

**Placement note:** the spec says "beside the other stream constants near `STREAM_TOKEN_GRACE`" (line 186). That is not possible: resolving the value logs a warning on a bad input, and `log` does not exist until line 194. Put the block immediately **after** `log` is created instead. Everything else in the spec holds.

- [ ] **Step 1: Write the failing tests**

Add this class to `tests/test_broker.py`, immediately before `class VerifyStreamDecisionTests`:

```python
class StreamGateResolveTests(unittest.TestCase):
    """STREAM_GATE parsing. Anything unrecognized has to land on 'off': the
    failure this switch exists to prevent is a stream nobody can reach, and a
    typo in the value must not be able to reintroduce it."""

    def test_known_modes_pass_through(self):
        self.assertEqual(broker._resolve_stream_gate("off"), "off")
        self.assertEqual(broker._resolve_stream_gate("token"), "token")

    def test_case_and_whitespace_are_normalized(self):
        self.assertEqual(broker._resolve_stream_gate("  TOKEN "), "token")

    def test_empty_uses_the_default(self):
        self.assertEqual(
            broker._resolve_stream_gate(""), broker.STREAM_GATE_DEFAULT
        )

    def test_unknown_value_falls_back_to_off_and_warns(self):
        with mock.patch.object(broker.log, "warning") as warn:
            self.assertEqual(broker._resolve_stream_gate("banana"), "off")
        warn.assert_called_once()

    def test_the_shipped_default_is_off(self):
        # Deliberate, and load bearing: see the spec. Released RomM cannot
        # send a stream token, so an enforcing default is a total lockout.
        # Flip this test and the default together when rommapp/romm#3856
        # merges, never the default alone.
        self.assertEqual(broker.STREAM_GATE_DEFAULT, "off")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_broker.StreamGateResolveTests -v`
Expected: FAIL, `AttributeError: module 'broker' has no attribute '_resolve_stream_gate'`

- [ ] **Step 3: Write the implementation**

In `root/root/broker.py`, insert directly after the `log = logging.getLogger("broker")` line and before the `if not _LD_PRELOAD_FROM_ENV:` block:

```python
# Stream gate enforcement. "token" enforces the nginx auth_request gate: a
# request reaches the desktop only if it carries the session token that
# POST /launch mints. "off" admits everything.
#
# The default is "off" because RomM has no way to send that token yet.
# rommapp/romm#3211 is merged and is what people are running, and its claim
# response hands the browser the operator's configured host with nothing
# appended; the half that carries the token through to the iframe URL is
# rommapp/romm#3856, still open. Enforcing against a client that cannot
# possibly comply refuses the document, every asset and the WebSocket upgrade
# alike, which is a total lockout rather than a gate. Set STREAM_GATE=token
# once #3856 ships, and see the README's Security section for what running
# with it off exposes.
#
# Declared here rather than beside STREAM_TOKEN_GRACE with the other stream
# constants because resolving the value can warn, and `log` does not exist
# that early in the module.
STREAM_GATE_MODES   = ("off", "token")
STREAM_GATE_DEFAULT = "off"


def _resolve_stream_gate(raw: str) -> str:
    """Normalize a STREAM_GATE value to one of STREAM_GATE_MODES.

    An unrecognized value resolves to "off" rather than to the current
    default. That is on purpose and does not track the default: a typo must
    fail toward a reachable stream, never toward one nobody can open.
    """
    mode = (raw or STREAM_GATE_DEFAULT).strip().lower()
    if mode not in STREAM_GATE_MODES:
        log.warning(
            "STREAM_GATE=%r is not one of %s, falling back to 'off', "
            "so the stream gate will not be enforced",
            raw,
            ", ".join(STREAM_GATE_MODES),
        )
        return "off"
    return mode


STREAM_GATE = _resolve_stream_gate(os.environ.get("STREAM_GATE", STREAM_GATE_DEFAULT))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_broker.StreamGateResolveTests -v`
Expected: PASS, 5 tests.

Then the full suite and the linter:

```bash
python3 -m unittest discover -s tests -q && ruff check .
```

Expected: all tests pass (119 now), `All checks passed!`. Nothing enforces the mode yet, so the 7 existing `VerifyStreamDecisionTests` must still pass untouched. If any of them fail here, you changed enforcement by mistake; revert that part.

- [ ] **Step 5: Commit**

```bash
git add root/root/broker.py tests/test_broker.py
git commit -m "feat(broker): resolve a STREAM_GATE mode from the environment

Parsing only, nothing enforces it yet. An unrecognized value resolves to
'off' rather than to whatever the default happens to be, because the
failure this switch exists to prevent is a stream nobody can reach and a
typo must not be able to reintroduce it.

Declared after the logger rather than beside STREAM_TOKEN_GRACE with the
other stream constants: resolving the value warns, and log does not exist
that early in the module."
```

---

### Task 2: Bypass enforcement when the gate is off

**Files:**
- Modify: `root/root/broker.py`, `_verify_stream_decision` (currently line 357)
- Test: `tests/test_broker.py`, existing `VerifyStreamDecisionTests` (line 801) plus a new class

**Interfaces:**
- Consumes: `STREAM_GATE` from Task 1.
- Produces: no new names. `_verify_stream_decision(original_uri: str, cookie_header: str | None) -> tuple[int, str | None, str | None]` keeps its existing signature and its `(status, set_cookie, reason)` return.

- [ ] **Step 1: Pin the existing gate tests to the enforcing mode**

The 7 tests in `VerifyStreamDecisionTests` all assert enforcement. Once Step 4 lands they would run under the new `off` default and pass vacuously, with the 403 assertions silently gone. Pin them first.

In `tests/test_broker.py`, replace the `setUp` of `VerifyStreamDecisionTests`:

```python
    def setUp(self):
        # These cover the enforcing path, so pin the mode. The shipped
        # default is "off" (see StreamGateResolveTests), under which every
        # 403 assertion below would pass for the wrong reason.
        gate = mock.patch.object(broker, "STREAM_GATE", "token")
        gate.start()
        self.addCleanup(gate.stop)
        broker._clear_stream_token()
        with broker._session_lock:
            broker._session["stream_token"] = "good"
            broker._session["stream_expires"] = time.monotonic() + 3600
```

Also update that class's docstring to name the mode:

```python
class VerifyStreamDecisionTests(unittest.TestCase):
    """The nginx auth_request decision under STREAM_GATE=token: 200 admits,
    403 rejects, and a query bootstrap hands back the stream_sid Set-Cookie."""
```

- [ ] **Step 2: Run them to confirm the pin changed nothing**

Run: `python3 -m unittest tests.test_broker.VerifyStreamDecisionTests -v`
Expected: PASS, 7 tests. Enforcement is still unconditional at this point, so pinning the mode is a no-op. A failure here means the patch target name is wrong.

- [ ] **Step 3: Write the failing tests for the off mode**

Add this class to `tests/test_broker.py` directly after `VerifyStreamDecisionTests`:

```python
class StreamGateOffTests(unittest.TestCase):
    """STREAM_GATE=off admits every request. The escape hatch exists for a
    RomM that cannot send a token at all (rommapp/romm#3856 unmerged), so the
    cases that matter are exactly the ones the gate would otherwise refuse."""

    def setUp(self):
        gate = mock.patch.object(broker, "STREAM_GATE", "off")
        gate.start()
        self.addCleanup(gate.stop)
        broker._clear_stream_token()

    def test_no_token_admits(self):
        # What released RomM actually sends: the bare configured host.
        status, cookie, reason = broker._verify_stream_decision("/", None)
        self.assertEqual(status, 200)
        self.assertIsNone(cookie)
        self.assertIsNone(reason)

    def test_garbage_cookie_admits(self):
        status, cookie, _ = broker._verify_stream_decision("/", "stream_sid=bad")
        self.assertEqual(status, 200)
        self.assertIsNone(cookie)

    def test_admits_with_no_session_open(self):
        # setUp cleared the token, so the enforcing path would answer
        # "no stream session is open" and refuse the whole iframe.
        status, _, reason = broker._verify_stream_decision("/websocket?x=1", None)
        self.assertEqual(status, 200)
        self.assertIsNone(reason)

    def test_a_valid_token_is_not_cookied_when_the_gate_is_off(self):
        # An operator already running the rommapp/romm#3856 branch can be
        # sending a good token while the gate is off. It is admitted like
        # everything else, and there is no session to hand the browser.
        with broker._session_lock:
            broker._session["stream_token"] = "good"
            broker._session["stream_expires"] = time.monotonic() + 3600
        status, cookie, _ = broker._verify_stream_decision(
            "/?stream_token=good", None
        )
        self.assertEqual(status, 200)
        self.assertIsNone(cookie)
```

- [ ] **Step 4: Run them to verify they fail**

Run: `python3 -m unittest tests.test_broker.StreamGateOffTests -v`
Expected: FAIL. `test_no_token_admits`, `test_garbage_cookie_admits` and `test_admits_with_no_session_open` fail with `403 != 200`. `test_a_valid_token_is_not_cookied_when_the_gate_is_off` fails on the cookie assertion, since the enforcing path still returns the `stream_sid` Set-Cookie.

- [ ] **Step 5: Write the implementation**

In `root/root/broker.py`, in `_verify_stream_decision`, insert the bypass as the first statement of the body, before `query = urlparse(original_uri).query`:

```python
    if STREAM_GATE == "off":
        # The switch is off: admit without reading the token. No Set-Cookie
        # either, because there is no gate for a cookie to satisfy later and
        # nginx would only rewrite the response for nothing.
        return 200, None, None
```

Then extend that function's docstring with a sentence naming the bypass, so the contract is not only discoverable from the body:

```python
    """Decide an nginx auth_request subrequest for the stream gate.

    Returns (status, set_cookie, reason). 200 admits the request, 403 rejects
    it and carries the reason so the refusal is legible in the container log.
    When the token arrives in the query (the first iframe load), the caller
    gets a Set-Cookie so later requests carry stream_sid and the token drops
    out of the URL. A cookie-authed request that is already good gets no
    Set-Cookie back, so nginx does not rewrite it.

    Under STREAM_GATE=off none of that runs and every request is admitted.
    """
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_broker.StreamGateOffTests tests.test_broker.VerifyStreamDecisionTests -v`
Expected: PASS, 11 tests. Both classes must be green: the off class proves the bypass works, the pinned class proves enforcement still works when it is asked for.

Then:

```bash
python3 -m unittest discover -s tests -q && ruff check .
```

Expected: all pass (123 now), `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
git add root/root/broker.py tests/test_broker.py
git commit -m "fix(stream): let STREAM_GATE=off admit the stream

8e3777b gates the desktop on a token RomM cannot send. rommapp/romm#3211
is merged and is what people run, and its claim response returns the
operator's configured host with nothing appended; the half that carries
the token is rommapp/romm#3856, still open. So the browser loads the
stream bare, /verify sees no token, and the document, every asset and the
WebSocket upgrade are all refused. That is a lockout, not a gate, and it
is live for everyone on the published image.

The decision is made here rather than in the nginx config on purpose. As
1736c84 recorded, that patch lands in the container's writable layer
behind its own marker grep, so a gate decided there needs a recreate in
each direction; decided in the broker, switching modes is one variable.

The token machinery is untouched: /launch still mints, /status still
reports, release still clears, and the TTL and grace still tick. Only
enforcement is bypassed, so turning the gate back on is one variable.

The seven existing gate tests are pinned to STREAM_GATE=token, or every
403 assertion in them would have started passing for the wrong reason
under the new default."
```

---

### Task 3: Report the active mode

Whether the desktop is currently reachable without a credential must not be something an operator has to infer.

**Files:**
- Modify: `root/root/broker.py` (new `_log_stream_gate_mode`, the `/status` payload at line 2256, and `main()` at line 2587)
- Test: `tests/test_broker.py`

**Interfaces:**
- Consumes: `STREAM_GATE` from Task 1.
- Produces:
  - `_log_stream_gate_mode() -> None`, called once from `main()`
  - `GET /status` gains a `"stream_gate"` key whose value is `"off"` or `"token"`

- [ ] **Step 1: Write the failing tests**

Add this class to `tests/test_broker.py` directly after `StreamGateOffTests`:

```python
class StreamGateReportingTests(unittest.TestCase):
    """Whether the desktop is open right now has to be answerable from the
    container log and from /status, without a shell in the container."""

    def _status_handler(self):
        h = broker.BrokerHandler.__new__(broker.BrokerHandler)
        h.path = "/status"
        h.command = "GET"
        h.headers = {}
        h.client_address = ("10.0.0.5", 51234)
        h.wfile = io.BytesIO()
        h.send_response = lambda *a, **k: None
        h.send_header = lambda *a, **k: None
        h.end_headers = lambda: None
        return h

    def _status_body(self, mode):
        h = self._status_handler()
        with mock.patch.object(broker, "STREAM_GATE", mode):
            h._handle_GET()
        return json.loads(h.wfile.getvalue().decode())

    def test_status_reports_the_enforcing_mode(self):
        self.assertEqual(self._status_body("token")["stream_gate"], "token")

    def test_status_reports_the_open_mode(self):
        self.assertEqual(self._status_body("off")["stream_gate"], "off")

    def test_off_warns_at_startup_naming_the_exposure_and_the_fix(self):
        with mock.patch.object(broker, "STREAM_GATE", "off"):
            with self.assertLogs("broker", level="WARNING") as cm:
                broker._log_stream_gate_mode()
        line = cm.output[0]
        self.assertIn("WARNING", line)
        self.assertIn("STREAM_GATE=token", line)
        self.assertIn("/files", line)

    def test_token_says_so_at_info_and_does_not_warn(self):
        with mock.patch.object(broker, "STREAM_GATE", "token"):
            with self.assertNoLogs("broker", level="WARNING"):
                with self.assertLogs("broker", level="INFO") as cm:
                    broker._log_stream_gate_mode()
        self.assertIn("enforced", cm.output[0])
```

Note: `broker.log.setLevel("CRITICAL")` runs at the top of the test file, but `assertLogs` sets the level it is given on the named logger for the duration of the block, so these assertions work regardless.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest tests.test_broker.StreamGateReportingTests -v`
Expected: FAIL. The two `_log_stream_gate_mode` tests fail with `AttributeError`; the two `/status` tests fail with `KeyError: 'stream_gate'`.

- [ ] **Step 3: Write the implementation**

Three edits in `root/root/broker.py`.

First, add the helper directly after the `_resolve_stream_gate` / `STREAM_GATE` block from Task 1:

```python
def _log_stream_gate_mode() -> None:
    """Announce stream gate enforcement at startup, in both directions.

    The permissive case names what is exposed and how to close it, because an
    operator should never have to deduce that the desktop is open. The
    enforcing case says so too, so "is the gate actually on" is answerable
    from `docker logs pcsx2` alone.
    """
    if STREAM_GATE == "token":
        log.info(
            "Stream gate enforced: the desktop admits only requests carrying "
            "the stream token that POST /launch mints"
        )
        return
    log.warning(
        "STREAM_GATE=off, the stream gate is NOT enforced: anyone who can reach "
        "port 3000 or 3001 gets the interactive desktop and the ROM library at "
        "/files, with no credential. This is the default while RomM has no way "
        "to send the token (rommapp/romm#3856). Set STREAM_GATE=token to close it."
    )
```

Second, in the `/status` payload (currently line 2256), add the key after `stream_token`:

```python
                "stream_token": _live_stream_token() if active else None,
                # Whether the nginx gate is actually being enforced. Reported
                # because "is the desktop open right now" should be answerable
                # remotely, and this endpoint is already behind BROKER_SECRET.
                "stream_gate": STREAM_GATE,
```

Third, in `main()`, call the helper right after the existing secret block (currently line 2584-2587), so both credentials are reported together:

```python
    if SECRET:
        log.info("Shared secret auth enabled")
    else:
        log.warning("BROKER_SECRET not set, all POST/DELETE endpoints are unauthenticated")
    _log_stream_gate_mode()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest tests.test_broker.StreamGateReportingTests -v`
Expected: PASS, 4 tests.

Then:

```bash
python3 -m unittest discover -s tests -q && ruff check .
```

Expected: all pass (127 now), `All checks passed!`.

- [ ] **Step 5: Commit**

```bash
git add root/root/broker.py tests/test_broker.py
git commit -m "fix(broker): report the stream gate mode at startup and on /status

Running with the gate off is a real exposure, so it is stated rather than
left to be deduced: the startup line names what is reachable (the
interactive desktop and the ROM library at /files) and names the variable
that closes it. The enforcing case logs too, so 'is the gate actually on'
is answerable from docker logs alone rather than by testing it.

/status carries the mode for the same reason, one step further out: that
endpoint is already behind BROKER_SECRET, so the answer is available
without a shell in the container."
```

---

### Task 4: Documentation

No code changes, so no tests. The README currently states the gate as an unconditional guarantee, which stops being true the moment Task 2 ships.

**Files:**
- Modify: `README.md` (compose example line 46, environment table after line 126, Security section lines 215-221)
- Modify: `root/etc/s6-overlay/s6-rc.d/init-pcsx2-config/init.sh` (comment block at lines 113-134 only)

**Interfaces:**
- Consumes: `STREAM_GATE` naming and semantics from Tasks 1 to 3.
- Produces: nothing consumed by later tasks. This is the last task.

- [ ] **Step 1: Add the environment table row**

In `README.md`, add after the `STREAM_TOKEN_GRACE` row (line 126):

```markdown
| `STREAM_GATE` | `off` | `token` enforces the stream gate: the desktop on 3000/3001 admits only requests carrying the token `POST /launch` mints. `off` admits everything. Defaults to `off` because released RomM cannot send that token yet, see [Security](#security). |
```

- [ ] **Step 2: Add it to the compose example**

In `README.md`, in the `environment:` block of the compose example, add after the `ROM_ROOT` line:

```yaml
      - STREAM_GATE=off        # see Security; 'token' once RomM sends the token
```

- [ ] **Step 3: Rewrite the Security paragraph**

In `README.md`, replace the paragraph beginning `**The stream token guards the desktop on 3000/3001.**` (line 217) with:

```markdown
**The stream token guards the desktop on 3000/3001, when you turn it on.** It is minted per session by `POST /launch`, 256 bits from `secrets.token_urlsafe`, and enforced by an nginx `auth_request` that the mod injects into *every* `server` block in the site config. That matters: the base image ships two identical vhosts, plain HTTP on 3000 and TLS on 3001, both proxying the same selkies stream and both serving `/config/Desktop` at `/files`. Gating only the TLS one left a complete bypass a port number away. The `stream_sid` cookie is `Secure`, so 3000 is usable only behind a TLS-terminating proxy: direct plain-HTTP browsing to it fails closed.

**The gate ships off, and that is a real hole.** With `STREAM_GATE=off`, anyone who can reach port 3000 or 3001 gets the interactive desktop with your ROM library browsable at `/files`, no credential asked for. It defaults to off because RomM cannot send the token yet. The streaming feature people are running is [rommapp/romm#3211](https://github.com/rommapp/romm/pull/3211), which is merged and hands the browser your configured `host` with nothing appended; the half that carries the token into the iframe URL is [rommapp/romm#3856](https://github.com/rommapp/romm/pull/3856), still open. Enforcing against a client that cannot comply is not a gate, it is a black stream: nginx refuses the document, every asset and the WebSocket upgrade alike, with nothing on screen to say why.

So: keep the container on a network you trust until #3856 ships, then set `STREAM_GATE=token` and bring the container back up:

```bash
docker compose up -d pcsx2
```

Use `up -d`, not `restart`. Compose bakes the environment in at create time, so `docker compose restart` replays the old value and the gate silently stays off. The changed variable is enough on its own to recreate the container; no `--force-recreate` is needed, because enforcement is decided in the broker rather than in the nginx config, and the init re-injects the same gate on the fresh layer either way. Confirm it took: the broker names the mode in its startup log, and `GET /status` reports it as `stream_gate`.
```

- [ ] **Step 4: Correct the init.sh comment**

In `root/etc/s6-overlay/s6-rc.d/init-pcsx2-config/init.sh`, the block at line 113 claims unconditional enforcement. Append this paragraph to it, directly after the line ending `that secret and the stream token is the credential.` (line 120), keeping the surrounding lines untouched:

```sh
#
# The gate is injected unconditionally, but ENFORCEMENT is the broker's call:
# /verify admits everything when STREAM_GATE=off, which is the default while
# RomM has no way to send the token. That split is deliberate. This file writes
# into the container's writable layer behind the marker grep below, so a mode
# decided here would need a --force-recreate in each direction. Decided in the
# broker it is one variable, and the marker grep makes this script idempotent,
# so the fresh layer a changed variable brings costs nothing to re-patch.
```

- [ ] **Step 5: Verify the prose**

```bash
grep -rnP '[\x{2010}-\x{2015}\x{2212}]' README.md root/ tests/ docs/
```
Expected: no output. Note line 217 of the README carries an em dash today that the earlier sweeps missed; the Step 3 rewrite removes it, so this check should come back clean rather than reporting a pre-existing hit.

```bash
grep -rn ' $' README.md root/etc/s6-overlay/s6-rc.d/init-pcsx2-config/init.sh
```
Expected: no output.

Then confirm nothing in the shell script broke:

```bash
sh -n root/etc/s6-overlay/s6-rc.d/init-pcsx2-config/init.sh && python3 -m unittest discover -s tests -q && ruff check .
```
Expected: no syntax error, all tests pass, `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
git add README.md root/etc/s6-overlay/s6-rc.d/init-pcsx2-config/init.sh
git commit -m "fix(stream): document that the stream gate ships off

The Security section stated the token gate as settled fact, which stopped
being true the moment STREAM_GATE landed. It now names the exposure in
its own paragraph rather than burying it in a variable table: with the
gate off, anyone reaching 3000 or 3001 gets the desktop and the ROM
library at /files with no credential.

It also names why, with both upstream PRs linked, because an operator
weighing that risk needs to know it ends when rommapp/romm#3856 merges
and that turning the gate on is one variable and an `up -d`.

The init.sh comment block claimed unconditional enforcement too. It now
records the split: the gate is always injected, the broker decides
whether to enforce, and that is what keeps a mode switch from needing a
--force-recreate in each direction."
```

---

## Verification

After Task 4, the whole change is in. Confirm:

```bash
python3 -m unittest discover -s tests -q && ruff check .
```
Expected: 127 tests pass, `All checks passed!`.

```bash
git log --oneline -4
```
Expected: the four commits above, newest first.

```bash
git log --format='%G? %s' -4
```
Expected: every line starts with `G`. Any `N` means a commit went in unsigned and must be re-signed with `git rebase --exec 'git commit --amend --no-edit -S' HEAD~4`.

The one thing the suite cannot prove is the container behavior, which matches this repo's existing test boundary (see the module docstring in `tests/test_broker.py`). Verify on the live container per the memory note `nuc-emulators-podman-access`:

1. Recreate `pcsx2` with the new mod image and no `STREAM_GATE` set.
2. `podman logs pcsx2 | grep -i "stream gate"` shows the `STREAM_GATE=off` warning.
3. `curl -sk -o /dev/null -w '%{http_code}\n' https://<host>:3001/` returns `200`, not `403`. This is the lockout being gone, and it is the whole point of the change.
4. `curl -s -H "X-Broker-Secret: $SECRET" http://<host>:8000/status | grep stream_gate` reports `"stream_gate": "off"`.
5. Set `STREAM_GATE=token`, bring the container back up with `up -d` (not `restart`, which replays the environment baked in at create time and would leave the old mode live), and confirm the same `curl` in step 3 now returns `403` while the startup log says the gate is enforced. One variable being enough, with no config-file surgery and no `--force-recreate`, is the design claim worth checking directly.

## When rommapp/romm#3856 merges

Not part of this plan, recorded so it is not rediscovered later:

1. Change `STREAM_GATE_DEFAULT` to `"token"` in `broker.py`.
2. Update `test_the_shipped_default_is_off` in `tests/test_broker.py`, including its name and comment. `_resolve_stream_gate`'s fallback to `"off"` on a bad value stays as it is, on purpose.
3. Update the README environment table row, the compose example and the Security section.
4. Say in the release notes that operators on an older RomM must set `STREAM_GATE=off` to keep streaming.
