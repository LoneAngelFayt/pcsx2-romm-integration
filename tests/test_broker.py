#!/usr/bin/env python3
"""Unit tests for broker.py.

Development-only, excluded from the Docker image via .dockerignore. Run from
the repo root with the stdlib runner (no pytest, no dependencies):

    python3 -m unittest discover -s tests -v

These cover the pure logic: save/memory-card archive handling, INI parsing,
and the session lifecycle state machine. Anything needing a real X display,
PINE socket, or pcsx2-qt process is out of scope and only provable on the
container.
"""

import io
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "root" / "root")
)

import broker  # noqa: E402

# The broker logs at INFO on import and throughout; tests assert on state, not
# output, so keep the runner readable.
broker.log.setLevel("CRITICAL")


def _reset_session(**overrides):
    """Return _session to a known idle state, then apply overrides."""
    broker._session.update({
        "process": None,
        "rom_path": None,
        "rom_name": None,
        "started_at": None,
        "is_managed": False,
        "save_in_progress": False,
        "launch_in_progress": False,
        "launch_seq": 0,
        "card_op_in_progress": False,
        "relaunch_abandoned": False,
        "current_slot": 1,
        "save_baseline": None,
    })
    broker._session.update(overrides)
    broker._rapid_exits = 0


class _TempRootMixin:
    """Gives each test an isolated PCSX2 data dir with chown stubbed out
    (the suite does not run as root)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        os.environ["SAVE_DATA_ROOT"] = str(self.root)
        self.addCleanup(os.environ.pop, "SAVE_DATA_ROOT", None)
        patcher = mock.patch.object(broker.os, "chown", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.memcards = self.root / "memcards"

    def _write(self, rel, data=b"x", mtime=None):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    @staticmethod
    def _zip(members):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in members.items():
                zf.writestr(name, data)
        return buf.getvalue()


class SaveArchiveTests(_TempRootMixin, unittest.TestCase):

    def test_round_trip_restores_content_and_tree(self):
        self._write("memcards/Mcd001/BASLUS-1/save.bin", b"G" * 2048)
        self._write("memcards/Mcd001/_pcsx2_superblock", b"S" * 512)
        archive, skipped = broker._build_save_archive(time.time() - 10)
        self.assertEqual(skipped, 0)

        __import__("shutil").rmtree(self.memcards)
        self.assertEqual(broker._extract_save_archive(archive), (2, 0, 0))
        self.assertEqual(
            (self.memcards / "Mcd001" / "BASLUS-1" / "save.bin").read_bytes(), b"G" * 2048
        )

    def test_only_files_newer_than_baseline_are_shipped(self):
        self._write("memcards/old.bin", b"old", mtime=time.time() - 3600)
        self._write("memcards/new.bin", b"new")
        archive, _ = broker._build_save_archive(time.time() - 60)
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            self.assertEqual(zf.namelist(), ["memcards/new.bin"])

    def test_no_changes_since_baseline_returns_none(self):
        self._write("memcards/a.bin", b"a", mtime=time.time() - 3600)
        self.assertIsNone(broker._build_save_archive(time.time()))

    def test_missing_data_dir_returns_none(self):
        os.environ["SAVE_DATA_ROOT"] = str(self.root / "nope")
        self.assertIsNone(broker._build_save_archive(0))

    def test_dot_prefixed_staging_files_are_never_swept_in(self):
        self._write("memcards/real.bin", b"r")
        self._write("memcards/.Mcd001.new-123/leftover.bin", b"junk")
        self._write("memcards/.hidden.tmp", b"junk")
        archive, _ = broker._build_save_archive(time.time() - 60)
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            self.assertEqual(zf.namelist(), ["memcards/real.bin"])

    def test_restore_skips_files_newer_than_archive(self):
        self._write("memcards/card.bin", b"archived")
        archive, _ = broker._build_save_archive(time.time() - 10)
        # Local copy is newer than the archive member: must not be rolled back.
        future = time.time() + 3600
        self._write("memcards/card.bin", b"LOCAL", mtime=future)
        written, skipped, failed = broker._extract_save_archive(archive)
        self.assertEqual((written, skipped, failed), (0, 1, 0))
        self.assertEqual((self.memcards / "card.bin").read_bytes(), b"LOCAL")

    def test_restore_is_timezone_independent(self):
        """Archives are stamped in UTC, so a TZ change between the pull and
        the restore must not shift mtimes and defeat the newer-file guard."""
        self._write("memcards/card.bin", b"archived")
        archive, _ = broker._build_save_archive(time.time() - 10)
        original = time.tzset if hasattr(time, "tzset") else None
        if original is None:
            self.skipTest("tzset unavailable")
        __import__("shutil").rmtree(self.memcards)
        os.environ["TZ"] = "Pacific/Kiritimati"  # UTC+14
        time.tzset()
        self.addCleanup(lambda: (os.environ.pop("TZ", None), time.tzset()))
        self.assertEqual(broker._extract_save_archive(archive), (1, 0, 0))
        stamped = (self.memcards / "card.bin").stat().st_mtime
        # Within DOS 2 s resolution of when we wrote it, not 14 h away.
        self.assertLess(abs(stamped - time.time()), 120)

    def test_rejects_path_traversal(self):
        result = broker._extract_save_archive(self._zip({"../../etc/passwd": b"x"}))
        self.assertIsInstance(result, str)
        self.assertIn("escapes save dir", result)

    def test_rejects_members_outside_allowed_subtrees(self):
        result = broker._extract_save_archive(self._zip({"sstates/evil.p2s": b"x"}))
        self.assertIsInstance(result, str)
        self.assertIn("outside save subtrees", result)

    def test_rejects_non_zip_body(self):
        self.assertIn("not a zip", broker._extract_save_archive(b"garbage"))

    def test_unstable_file_is_skipped_not_shipped_torn(self):
        self._write("memcards/card.bin", b"x" * 100)
        with mock.patch.object(broker, "_read_file_stable", return_value=None):
            result = broker._build_save_archive(time.time() - 10)
        # Every changed file was mid-write: no archive, caller must refuse.
        self.assertEqual(result, (None, 1))


class MemoryCardTests(_TempRootMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        ini = self.root / "PCSX2.ini"
        ini.write_text("[MemoryCards]\nSlot1_Filename = Mcd001\n")
        patcher = mock.patch.object(broker, "INI_PATH", ini)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.card = self.memcards / "Mcd001"

    def test_reads_slot1_name_from_ini(self):
        self.assertEqual(broker._slot1_filename(), "Mcd001")
        self.assertEqual(broker._slot1_card_path(), self.card)

    def test_slot1_name_is_basename_only(self):
        (self.root / "PCSX2.ini").write_text(
            "[MemoryCards]\nSlot1_Filename = ../../escape/Mcd001\n"
        )
        self.assertEqual(broker._slot1_card_path().parent, self.memcards)

    def test_archive_members_are_relative_to_card_root(self):
        self._write("memcards/Mcd001/BASLUS-1/save.bin", b"G")
        self._write("memcards/Mcd001/_pcsx2_superblock", b"S")
        with zipfile.ZipFile(io.BytesIO(broker._build_memory_card_archive())) as zf:
            self.assertEqual(
                sorted(zf.namelist()), ["BASLUS-1/save.bin", "_pcsx2_superblock"]
            )

    def test_replace_wipes_the_whole_card(self):
        self._write("memcards/Mcd001/BASLUS-1/old.bin", b"O")
        self._write("memcards/Mcd001/_pcsx2_superblock", b"S")
        payload = self._zip({"_pcsx2_superblock": b"T", "BASLUS-2/new.bin": b"N"})
        self.assertEqual(broker._replace_memory_card(payload), (2,))
        present = sorted(p.relative_to(self.card).as_posix() for p in self.card.rglob("*"))
        self.assertEqual(present, ["BASLUS-2", "BASLUS-2/new.bin", "_pcsx2_superblock"])

    def test_replace_leaves_no_staging_or_backup_dirs(self):
        self._write("memcards/Mcd001/_pcsx2_superblock", b"S")
        broker._replace_memory_card(self._zip({"_pcsx2_superblock": b"T"}))
        leftovers = [p.name for p in self.memcards.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_rejected_traversal_leaves_card_intact(self):
        self._write("memcards/Mcd001/_pcsx2_superblock", b"S")
        result = broker._replace_memory_card(self._zip({"../escape": b"x"}))
        self.assertIn("escapes card dir", result)
        self.assertEqual((self.card / "_pcsx2_superblock").read_bytes(), b"S")

    def test_file_card_is_refused_both_directions(self):
        self.memcards.mkdir(parents=True, exist_ok=True)
        self.card.write_bytes(b"file card")
        self.assertIn("File memory card", broker._build_memory_card_archive())
        self.assertIn("File memory card", broker._replace_memory_card(self._zip({"a": b"b"})))

    def test_absent_card_reports_none_not_error(self):
        """A missing card must be distinguishable from a broken one: the
        handler turns None into the tagged 404 the backend keys on."""
        self.assertIsNone(broker._build_memory_card_archive())

    def test_no_slot_configured_is_an_error_on_replace(self):
        (self.root / "PCSX2.ini").write_text("[MemoryCards]\n")
        self.assertIn("no Slot 1", broker._replace_memory_card(self._zip({"a": b"b"})))


class IniTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.ini = self.tmp / "PCSX2.ini"
        patcher = mock.patch.object(broker, "INI_PATH", self.ini)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_patch_overwrites_existing_key_in_correct_section(self):
        self.ini.write_text("[EmuCore]\nEnablePINE = false\n")
        broker._patch_ini()
        self.assertIn("EnablePINE = true", self.ini.read_text())

    def test_patch_does_not_touch_same_key_in_another_section(self):
        self.ini.write_text("[Other]\nEnablePINE = false\n\n[EmuCore]\nEnablePINE = false\n")
        broker._patch_ini()
        other, emucore = self.ini.read_text().split("[EmuCore]")
        self.assertIn("EnablePINE = false", other)
        self.assertIn("EnablePINE = true", emucore)

    def test_patch_creates_missing_sections(self):
        self.ini.write_text("[Unrelated]\nx = 1\n")
        broker._patch_ini()
        text = self.ini.read_text()
        self.assertIn("[UI]", text)
        self.assertIn("StartFullscreen = true", text)
        self.assertIn("ConfirmShutdown = false", text)

    def test_patch_is_idempotent(self):
        self.ini.write_text("[EmuCore]\nEnablePINE = false\n")
        broker._patch_ini()
        first = self.ini.read_text()
        broker._patch_ini()
        self.assertEqual(first, self.ini.read_text())


class InitialSlotTests(unittest.TestCase):
    """The seed comes from the env only. PCSX2 2.6.3 never writes a
    SaveStateSlot key to its ini, so there is nothing on disk to read."""

    def test_reads_env_var(self):
        with mock.patch.dict(os.environ, {"BROKER_INITIAL_SLOT": "7"}):
            self.assertEqual(broker._initial_save_slot(), 7)

    def test_defaults_to_one_when_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(broker._initial_save_slot(), 1)

    def test_falls_back_when_out_of_range(self):
        with mock.patch.dict(os.environ, {"BROKER_INITIAL_SLOT": "99"}):
            self.assertEqual(broker._initial_save_slot(), 1)

    def test_falls_back_when_not_a_number(self):
        with mock.patch.dict(os.environ, {"BROKER_INITIAL_SLOT": "seven"}):
            self.assertEqual(broker._initial_save_slot(), 1)


class SlotMatchTests(unittest.TestCase):

    def test_matches_padded_and_bare_slot_suffixes(self):
        self.assertTrue(broker._matches_slot(Path("SLUS-1 (A).03.p2s"), 3))
        self.assertTrue(broker._matches_slot(Path("SLUS-1 (A).3.p2s"), 3))
        self.assertTrue(broker._matches_slot(Path("SLUS-1 (A).10.p2s"), 10))

    def test_does_not_match_other_slots(self):
        self.assertFalse(broker._matches_slot(Path("SLUS-1 (A).04.p2s"), 3))
        self.assertFalse(broker._matches_slot(Path("SLUS-1 (A).03.p2s"), 10))


class _FakeProc:
    """Stands in for a pcsx2-qt Popen that has already exited."""
    pid = 4242

    def wait(self):
        return 0

    def poll(self):
        return 0


class LifecycleTests(unittest.TestCase):

    def setUp(self):
        _reset_session()
        self.addCleanup(_reset_session)
        self.launched = []
        self.launch_patcher = mock.patch.object(
            broker, "_launch_pcsx2_internal", lambda rom: self.launched.append(rom)
        )
        self.launch_patcher.start()
        self.addCleanup(self.launch_patcher.stop)
        # Collapse the crash-relaunch backoff so tests don't sleep.
        sleep_patch = mock.patch.object(broker.time, "sleep", lambda _s: None)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        self.proc = _FakeProc()

    def _crash(self, ran_for=60.0):
        broker._session.update({"process": self.proc, "is_managed": True})
        broker._monitor_process(self.proc, time.monotonic() - ran_for)

    def test_unexpected_exit_relaunches_dashboard(self):
        broker._session["rom_path"] = "/romm/library/ps2/game.chd"
        self._crash()
        self.assertEqual(self.launched, [None])
        self.assertIsNone(broker._session["rom_path"])
        self.assertEqual(broker._session["rom_name"], "Dashboard")

    def test_deliberate_kill_does_not_relaunch(self):
        broker._session.update({"process": None, "is_managed": False})
        broker._monitor_process(self.proc, time.monotonic() - 60)
        self.assertEqual(self.launched, [])

    def test_crash_loop_gives_up_and_flags_status(self):
        for _ in range(broker._CRASH_LOOP_LIMIT + 1):
            broker._session["launch_in_progress"] = False
            self._crash(ran_for=0.5)
        self.assertEqual(len(self.launched), broker._CRASH_LOOP_LIMIT - 1)
        self.assertTrue(broker._session["relaunch_abandoned"])

    def test_deliberate_kills_do_not_count_toward_crash_loop(self):
        for _ in range(5):
            broker._session.update({"process": None, "is_managed": False})
            broker._monitor_process(self.proc, time.monotonic() - 0.5)
        self.assertEqual(broker._rapid_exits, 0)

    def test_long_run_resets_the_crash_counter(self):
        self._crash(ran_for=0.5)
        broker._session["launch_in_progress"] = False
        self._crash(ran_for=600)
        self.assertEqual(broker._rapid_exits, 0)

    def test_crash_relaunch_skipped_while_a_launch_holds_the_claim(self):
        self.assertTrue(broker._claim_launch())
        self._crash()
        self.assertEqual(self.launched, [])

    def test_card_hydrate_does_not_block_crash_recovery(self):
        """Regression: the card op used to borrow launch_in_progress, so a
        crash during a hydrate left no emulator running and nothing retried."""
        self.assertTrue(broker._claim_card_op())
        self._crash()
        self.assertEqual(self.launched, [None])

    def test_card_op_refused_while_a_game_is_running(self):
        broker._session["rom_path"] = "/romm/library/ps2/game.chd"
        self.assertFalse(broker._claim_card_op())

    def test_card_op_refused_while_a_launch_is_in_flight(self):
        self.assertTrue(broker._claim_launch())
        self.assertFalse(broker._claim_card_op())

    def test_card_op_is_exclusive(self):
        self.assertTrue(broker._claim_card_op())
        self.assertFalse(broker._claim_card_op())
        broker._release_card_op()
        self.assertTrue(broker._claim_card_op())

    def test_launch_claim_is_exclusive(self):
        self.assertTrue(broker._claim_launch())
        self.assertFalse(broker._claim_launch())

    def test_successful_launch_clears_the_abandoned_flag(self):
        """Once an instance is up again, the limiter's verdict is stale and
        /status must stop reporting the container as surrendered."""
        broker._session["relaunch_abandoned"] = True
        self.addCleanup(self.launch_patcher.start)
        self.launch_patcher.stop()  # exercise the real _launch_pcsx2_internal
        log_path = Path(tempfile.mkdtemp()) / "pcsx2-qt.log"
        with mock.patch.object(broker, "PCSX2_LOG_PATH", log_path), \
             mock.patch.object(broker.subprocess, "Popen", return_value=self.proc), \
             mock.patch.object(broker, "_initial_save_slot", return_value=1), \
             mock.patch.object(broker, "Thread") as thread:
            thread.return_value.start.return_value = None
            broker._launch_pcsx2_internal(None)
        self.assertFalse(broker._session["relaunch_abandoned"])
        self.assertIs(broker._session["process"], self.proc)
        self.assertTrue(broker._session["is_managed"])


class GpuEnvTests(unittest.TestCase):
    """sudo's env_reset wipes the container environment, so anything the
    operator sets for the GPU only reaches pcsx2-qt if the broker names it
    explicitly on the `env` command line."""

    def test_forwards_vendor_graphics_variables(self):
        with mock.patch.dict(os.environ, {
            "VK_DRIVER_FILES": "/usr/share/vulkan/icd.d/nvidia_icd.json",
            "__GLX_VENDOR_LIBRARY_NAME": "nvidia",
            "NVIDIA_DRIVER_CAPABILITIES": "all",
            "MESA_VK_DEVICE_SELECT": "10de:0000",
        }, clear=False):
            env = broker._gpu_env()
        self.assertEqual(
            env["VK_DRIVER_FILES"], "/usr/share/vulkan/icd.d/nvidia_icd.json"
        )
        self.assertEqual(env["__GLX_VENDOR_LIBRARY_NAME"], "nvidia")
        self.assertEqual(env["NVIDIA_DRIVER_CAPABILITIES"], "all")
        self.assertEqual(env["MESA_VK_DEVICE_SELECT"], "10de:0000")

    def test_ignores_unrelated_and_empty_variables(self):
        with mock.patch.dict(os.environ, {
            "BROKER_SECRET": "hunter2",
            "PATH": "/usr/bin",
            "VK_DRIVER_FILES": "",
        }, clear=False):
            env = broker._gpu_env()
        self.assertNotIn("BROKER_SECRET", env)
        self.assertNotIn("PATH", env)
        self.assertNotIn("VK_DRIVER_FILES", env)

    def test_computed_entries_win_over_inherited_ones(self):
        """DISPLAY and LD_PRELOAD are derived from live container state; a
        stale inherited value must never shadow them."""
        self.assertEqual(broker.ENV["DISPLAY"], broker._detect_display())
        self.assertEqual(broker.ENV["LD_PRELOAD"], broker._LD_PRELOAD)

    def test_launch_puts_gpu_env_on_the_sudo_command_line(self):
        _reset_session()
        self.addCleanup(_reset_session)
        log_path = Path(tempfile.mkdtemp()) / "pcsx2-qt.log"
        env = dict(broker.ENV, VK_DRIVER_FILES="/icd/nvidia_icd.json")
        with mock.patch.object(broker, "ENV", env), \
             mock.patch.object(broker, "PCSX2_LOG_PATH", log_path), \
             mock.patch.object(broker.subprocess, "Popen") as popen, \
             mock.patch.object(broker, "_initial_save_slot", return_value=1), \
             mock.patch.object(broker, "Thread") as thread:
            thread.return_value.start.return_value = None
            broker._launch_pcsx2_internal(None)
        self.assertIn("VK_DRIVER_FILES=/icd/nvidia_icd.json", popen.call_args[0][0])


class DeferredLoadTests(unittest.TestCase):

    def setUp(self):
        _reset_session()
        self.addCleanup(_reset_session)
        sleep_patch = mock.patch.object(broker.time, "sleep", lambda _s: None)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def test_abandons_when_a_newer_launch_supersedes_it(self):
        broker._session["launch_seq"] = 5
        with mock.patch.object(broker, "_load_state") as load:
            broker._deferred_load_state(3, seq=4)  # stale generation
        load.assert_not_called()

    def test_loads_when_generation_still_matches(self):
        broker._session["launch_seq"] = 4
        with mock.patch.object(broker, "_pine_emu_status", return_value=0), \
             mock.patch.object(broker, "_load_state", return_value=True) as load:
            broker._deferred_load_state(3, seq=4)
        load.assert_called_once_with(3)

    def test_gives_up_when_vm_never_reaches_running(self):
        broker._session["launch_seq"] = 1
        with mock.patch.object(broker, "RESUME_LOAD_WAIT", 0.01), \
             mock.patch.object(broker, "_pine_emu_status", return_value=2), \
             mock.patch.object(broker, "_load_state") as load:
            broker._deferred_load_state(3, seq=1)
        load.assert_not_called()


class _RomRootMixin:
    """Gives each test an isolated ROM_ROOT to build library layouts in."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        patcher = mock.patch.object(broker, "ROM_ROOT", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _rom(self, rel, data=b"disc"):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p


class RomPathValidationTests(_RomRootMixin, unittest.TestCase):

    def test_accepts_path_under_rom_root(self):
        p = self.tmp / "ps2" / "game.chd"
        self.assertEqual(broker._validate_rom_path(str(p)), p)

    def test_rejects_path_outside_rom_root(self):
        self.assertIsNone(broker._validate_rom_path("/etc/passwd"))

    def test_rejects_traversal_escape(self):
        self.assertIsNone(
            broker._validate_rom_path(str(self.tmp / ".." / "escape.chd"))
        )


class RomFileResolutionTests(_RomRootMixin, unittest.TestCase):
    """Issue #11: RomM addresses a folder-organized game by its folder, so
    /launch receives a directory pcsx2-qt cannot boot."""

    def test_plain_file_passes_through(self):
        iso = self._rom("ps2/game.iso")
        self.assertEqual(broker._resolve_rom_file(iso), iso)

    def test_game_folder_resolves_to_the_disc_image_inside(self):
        iso = self._rom("ps2/Jak 3/Jak 3.iso")
        self.assertEqual(broker._resolve_rom_file(self.tmp / "ps2" / "Jak 3"), iso)

    def test_folder_without_a_bootable_file_returns_none(self):
        self._rom("ps2/Jak 3/cover.jpg")
        self._rom("ps2/Jak 3/notes.txt")
        self.assertIsNone(broker._resolve_rom_file(self.tmp / "ps2" / "Jak 3"))

    def test_multi_disc_folder_picks_disc_one(self):
        disc1 = self._rom("ps2/FFX/FFX (Disc 1).iso")
        self._rom("ps2/FFX/FFX (Disc 2).iso")
        self.assertEqual(broker._resolve_rom_file(self.tmp / "ps2" / "FFX"), disc1)

    def test_better_format_wins_over_a_raw_track(self):
        chd = self._rom("ps2/Game/Game.chd")
        self._rom("ps2/Game/Game (Track 1).bin")
        self.assertEqual(broker._resolve_rom_file(self.tmp / "ps2" / "Game"), chd)

    def test_cue_sheet_is_not_chosen_over_its_image(self):
        binf = self._rom("ps2/Game/Game.bin")
        self._rom("ps2/Game/Game.cue")
        self.assertEqual(broker._resolve_rom_file(self.tmp / "ps2" / "Game"), binf)

    def test_finds_image_in_a_per_disc_subfolder(self):
        disc1 = self._rom("ps2/FFX/Disc 1/FFX.iso")
        self._rom("ps2/FFX/Disc 2/FFX.iso")
        self.assertEqual(broker._resolve_rom_file(self.tmp / "ps2" / "FFX"), disc1)

    def test_top_level_image_wins_over_a_nested_one(self):
        top = self._rom("ps2/Game/Game.iso")
        self._rom("ps2/Game/extras/bonus.iso")
        self.assertEqual(broker._resolve_rom_file(self.tmp / "ps2" / "Game"), top)

    def test_stray_track_at_the_top_does_not_beat_a_nested_disc_image(self):
        """A .bin loose in the game folder must not win just for being shallow:
        sets ship extras with bootable extensions, and the real discs are one
        level down in per-disc subfolders."""
        disc1 = self._rom("ps2/FFX/Disc 1/FFX (Disc 1).chd")
        self._rom("ps2/FFX/Disc 2/FFX (Disc 2).chd")
        self._rom("ps2/FFX/manual.bin")
        self.assertEqual(broker._resolve_rom_file(self.tmp / "ps2" / "FFX"), disc1)

    def test_disc_one_wins_even_when_disc_two_is_a_better_format(self):
        """Format preference decides which of two candidates for the *same*
        disc to boot. It must not decide which disc to start on."""
        disc1 = self._rom("ps2/FFX/Disc 1/FFX.iso")
        self._rom("ps2/FFX/Disc 2/FFX.chd")
        self.assertEqual(broker._resolve_rom_file(self.tmp / "ps2" / "FFX"), disc1)

    def test_disc_two_beats_disc_ten(self):
        """Disc order is numeric. Sorting the names as text puts 'Disc 10'
        ahead of 'Disc 2', which starts a long set on the wrong disc."""
        disc2 = self._rom("ps2/Game/Game (Disc 2).iso")
        self._rom("ps2/Game/Game (Disc 10).iso")
        self.assertEqual(broker._resolve_rom_file(self.tmp / "ps2" / "Game"), disc2)

    def test_unmarked_image_still_wins_over_a_nested_marked_one(self):
        """Nothing names a disc for the top-level image, so it must not lose to
        a deeper file that happens to carry a disc marker."""
        top = self._rom("ps2/Game/Game.iso")
        self._rom("ps2/Game/extras/Bonus (Disc 1).iso")
        self.assertEqual(broker._resolve_rom_file(self.tmp / "ps2" / "Game"), top)

    def test_does_not_descend_past_the_second_level(self):
        self._rom("ps2/Game/a/b/deep.iso")
        self.assertIsNone(broker._resolve_rom_file(self.tmp / "ps2" / "Game"))

    def test_hidden_files_are_ignored(self):
        self._rom("ps2/Game/._Game.iso")
        self.assertIsNone(broker._resolve_rom_file(self.tmp / "ps2" / "Game"))

    def test_symlink_escaping_rom_root_is_refused(self):
        outside = Path(tempfile.mkdtemp()).resolve() / "escape.iso"
        self.addCleanup(
            lambda: __import__("shutil").rmtree(outside.parent, ignore_errors=True)
        )
        outside.write_bytes(b"disc")
        folder = self.tmp / "ps2" / "Game"
        folder.mkdir(parents=True)
        (folder / "link.iso").symlink_to(outside)
        self.assertIsNone(broker._resolve_rom_file(folder))

    def test_missing_path_returns_none(self):
        self.assertIsNone(broker._resolve_rom_file(self.tmp / "ps2" / "nope"))


class _FakeHandler(broker.BrokerHandler):
    """Drives do_POST without a socket: the request line and body are given
    up front and the response is captured instead of written."""

    def __init__(self, path, body):
        self.path = path
        self._body = body
        self.sent = None

    def _check_secret(self):
        return True

    def _read_body(self):
        return self._body

    def _send_json(self, code, body, headers=None):
        self.sent = (code, body)


class LaunchEndpointTests(_RomRootMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        _reset_session()
        self.addCleanup(_reset_session)

    def _post_launch(self, rom_path):
        handler = _FakeHandler("/launch", {"rom_path": str(rom_path)})
        with mock.patch.object(broker, "Thread") as thread:
            handler.do_POST()
        self.thread = thread
        return handler.sent

    def test_launching_a_game_folder_boots_the_file_inside(self):
        iso = self._rom("ps2/Jak 3/Jak 3.iso")
        code, body = self._post_launch(self.tmp / "ps2" / "Jak 3")
        self.assertEqual(code, 200)
        self.assertEqual(body["rom_path"], str(iso))
        self.assertEqual(self.thread.call_args.kwargs["args"][0], str(iso))

    def test_folder_without_a_bootable_file_is_reported_distinctly(self):
        self._rom("ps2/Jak 3/cover.jpg")
        code, body = self._post_launch(self.tmp / "ps2" / "Jak 3")
        self.assertEqual(code, 422)
        self.assertIn("no bootable ROM file", body["error"])
        self.assertFalse(broker._session["launch_in_progress"])

    def test_missing_path_still_reports_that_it_does_not_exist(self):
        code, body = self._post_launch(self.tmp / "ps2" / "nope")
        self.assertEqual(code, 422)
        self.assertEqual(body["error"], "rom_path does not exist")


class StreamTokenTests(unittest.TestCase):
    def setUp(self):
        with broker._session_lock:
            broker._session["stream_token"] = None

    def test_issue_returns_nonempty_and_stores(self):
        tok = broker._issue_stream_token()
        self.assertTrue(tok)
        self.assertEqual(broker._session["stream_token"], tok)

    def test_check_true_for_issued_token(self):
        tok = broker._issue_stream_token()
        self.assertTrue(broker._check_stream_token(tok))

    def test_check_false_for_wrong_token(self):
        broker._issue_stream_token()
        self.assertFalse(broker._check_stream_token("nope"))

    def test_check_false_when_no_token_set(self):
        self.assertFalse(broker._check_stream_token("anything"))
        self.assertFalse(broker._check_stream_token(""))

    def test_clear_invalidates(self):
        tok = broker._issue_stream_token()
        broker._clear_stream_token()
        self.assertIsNone(broker._session["stream_token"])
        self.assertFalse(broker._check_stream_token(tok))

    def test_reissue_replaces_previous_token(self):
        first = broker._issue_stream_token()
        second = broker._issue_stream_token()
        self.assertNotEqual(first, second)
        self.assertFalse(broker._check_stream_token(first))
        self.assertTrue(broker._check_stream_token(second))


class StreamProxyHelperTests(unittest.TestCase):
    def test_extract_token_from_query(self):
        self.assertEqual(
            broker._extract_stream_token("stream_token=abc&x=1", None), "abc"
        )

    def test_extract_token_from_cookie(self):
        self.assertEqual(
            broker._extract_stream_token("", "stream_sid=abc; other=1"), "abc"
        )

    def test_query_beats_cookie(self):
        self.assertEqual(
            broker._extract_stream_token("stream_token=q", "stream_sid=c"), "q"
        )

    def test_extract_none_when_absent(self):
        self.assertIsNone(broker._extract_stream_token("y=2", "foo=bar"))
        self.assertIsNone(broker._extract_stream_token("", None))

    def test_cookie_value_has_required_attributes(self):
        v = broker._stream_cookie_value("abc")
        self.assertEqual(
            v,
            "stream_sid=abc; HttpOnly; Secure; SameSite=None; Partitioned; Path=/",
        )


class VerifyStreamDecisionTests(unittest.TestCase):
    """The nginx auth_request decision: 200 admits, 403 rejects, and a query
    bootstrap hands back the stream_sid Set-Cookie."""

    def setUp(self):
        with broker._session_lock:
            broker._session["stream_token"] = "good"

    def test_no_token_is_403(self):
        status, cookie = broker._verify_stream_decision("/", None)
        self.assertEqual(status, 403)
        self.assertIsNone(cookie)

    def test_valid_query_token_admits_and_sets_cookie(self):
        status, cookie = broker._verify_stream_decision(
            "/?stream_token=good", None
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            cookie,
            "stream_sid=good; HttpOnly; Secure; SameSite=None; Partitioned; Path=/",
        )

    def test_valid_cookie_admits_without_set_cookie(self):
        status, cookie = broker._verify_stream_decision(
            "/", "stream_sid=good; other=1"
        )
        self.assertEqual(status, 200)
        self.assertIsNone(cookie)

    def test_wrong_query_token_is_403(self):
        status, cookie = broker._verify_stream_decision(
            "/?stream_token=bad", None
        )
        self.assertEqual(status, 403)
        self.assertIsNone(cookie)

    def test_wrong_cookie_is_403(self):
        status, _ = broker._verify_stream_decision("/", "stream_sid=bad")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
