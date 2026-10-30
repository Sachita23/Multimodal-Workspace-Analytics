"""
Headless focus sessions show the Focus Assistant's deviation popup
(focus/overlay.py) like the Tk assistant did: on while off task, off
again once back in the zone or idle, closed when the session ends.
"""

import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from focus import engine as engine_module
from focus import notify
from focus import overlay as overlay_module
from focus.engine import FocusEngine
from tests.test_focus_engine import CHROME, CODE, SLACK, Clock, Notifier, Screen, run


class FakeOverlay:
    def __init__(self):
        self.on_action = None
        self.calls = []
        self.pid = 9999

    def pids(self):
        return {self.pid}

    def show(self, app, window_title, goal, allowed_apps):
        self.calls.append(("show", app, goal, tuple(allowed_apps)))

    def hide(self):
        self.calls.append(("hide",))

    def close(self):
        self.calls.append(("close",))

    def shown(self):
        return [call for call in self.calls if call[0] == "show"]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    # An engine with a popup turns notify's fallback popup off; undo that.
    monkeypatch.setattr(notify, "_fallback_popup", True)
    clock, screen, overlay = Clock(), Screen(), FakeOverlay()
    engine = FocusEngine(
        data_dir=tmp_path / "focus", sampler=screen.sampler, idle_reader=screen.idle_reader,
        notifier=Notifier(), clock=clock, tick_seconds=0.02, overlay=overlay,
    )
    screen.show(CODE)
    engine.start("Write", 25, [CODE], goal="Finish the report")
    run(engine, clock, screen, [(engine_module.START_GRACE_SECONDS + 1, CODE)])
    overlay.calls.clear()
    yield engine, clock, screen, overlay
    engine.stop()


def test_popup_shows_while_off_task_and_hides_back_in_the_zone(rig):
    engine, clock, screen, overlay = rig

    run(engine, clock, screen, [(3, SLACK)])
    assert overlay.shown()[-1] == ("show", SLACK, "Finish the report", (CODE,))

    run(engine, clock, screen, [(1, CODE)])
    assert overlay.calls[-1] == ("hide",)


def test_going_idle_hides_the_popup(rig):
    engine, clock, screen, overlay = rig

    run(engine, clock, screen, [(2, SLACK), (1, SLACK, "window", 600.0)])

    assert overlay.calls[-1] == ("hide",)


def test_looking_at_or_clicking_the_popup_is_not_a_deviation(rig):
    engine, clock, screen, overlay = rig
    run(engine, clock, screen, [(2, SLACK)])
    deviations = engine.live()["deviations"]

    # The popup (its own process) comes to the front.
    screen.pid = overlay.pid
    payloads = run(engine, clock, screen, [(5, "Python", "")])

    assert {p["state"] for p in payloads} == {"self"}
    assert engine.live()["deviations"] == deviations


def test_allow_button_adds_the_app_to_the_zone(rig):
    engine, clock, screen, overlay = rig
    run(engine, clock, screen, [(2, SLACK)])

    overlay.on_action("allow", SLACK)
    payloads = run(engine, clock, screen, [(2, SLACK)])

    assert SLACK in engine.live()["allowed_apps"]
    assert payloads[-1]["state"] == "allowed"
    assert overlay.calls[-1] == ("hide",)


def test_snooze_hides_the_popup_for_five_minutes_but_still_scores(rig):
    engine, clock, screen, overlay = rig
    run(engine, clock, screen, [(2, SLACK)])
    score = engine.live()["score"]

    overlay.on_action("snooze", None)
    overlay.calls.clear()
    run(engine, clock, screen, [(60, SLACK)])

    assert overlay.shown() == []
    assert engine.live()["score"] < score

    clock.advance(engine_module.SNOOZE_SECONDS)
    run(engine, clock, screen, [(1, SLACK)])
    assert overlay.shown()


def test_ending_the_session_closes_the_popup(rig):
    engine, clock, screen, overlay = rig
    run(engine, clock, screen, [(2, SLACK)])

    engine.stop()

    assert overlay.calls[-1] == ("close",)


def test_default_overlay_prefers_the_native_macos_popup(tmp_path, monkeypatch):
    monkeypatch.setenv(overlay_module.POPUP_ENV, "1")

    # Native helper available (macOS with clang): it is used, even without Tk.
    monkeypatch.setattr(overlay_module, "native_helper", lambda: tmp_path / "focus_popup-x")
    monkeypatch.setattr(overlay_module, "tk_available", lambda: False)
    controller = overlay_module.default_overlay()
    assert controller.command == [str(tmp_path / "focus_popup-x")]

    # No native helper: the Tk helper, if Tk is installed.
    monkeypatch.setattr(overlay_module, "native_helper", lambda: None)
    assert overlay_module.default_overlay() is None
    monkeypatch.setattr(overlay_module, "tk_available", lambda: True)
    controller = overlay_module.default_overlay()
    assert controller.command[-2:] == ["-m", "focus.overlay"]

    monkeypatch.setenv(overlay_module.POPUP_ENV, "0")
    assert overlay_module.default_overlay() is None


def test_native_helper_is_built_once_per_source_version(tmp_path, monkeypatch):
    source = tmp_path / "macos_popup.m"
    source.write_text("int main(void) { return 0; }")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        Path(argv[argv.index("-o") + 1]).write_text("binary")
        Path(argv[argv.index("-o") + 1]).chmod(0o755)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(overlay_module.shutil, "which", lambda name: "/usr/bin/clang")
    monkeypatch.setattr(overlay_module.subprocess, "run", fake_run)

    first = overlay_module.native_helper(source, tmp_path / "bin", platform="darwin")
    second = overlay_module.native_helper(source, tmp_path / "bin", platform="darwin")
    assert first == second and first.exists() and len(calls) == 1

    # A changed source builds a new binary.
    source.write_text("int main(void) { return 1; }")
    third = overlay_module.native_helper(source, tmp_path / "bin", platform="darwin")
    assert third != first and len(calls) == 2


def test_native_helper_needs_macos_and_a_working_compiler(tmp_path, monkeypatch):
    source = tmp_path / "macos_popup.m"
    source.write_text("broken")

    assert overlay_module.native_helper(source, tmp_path / "bin", platform="win32") is None

    monkeypatch.setattr(overlay_module.shutil, "which", lambda name: None)
    assert overlay_module.native_helper(source, tmp_path / "bin", platform="darwin") is None

    monkeypatch.setattr(overlay_module.shutil, "which", lambda name: "/usr/bin/clang")
    monkeypatch.setattr(overlay_module.subprocess, "run",
                        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1))
    assert overlay_module.native_helper(source, tmp_path / "bin", platform="darwin") is None
    assert list((tmp_path / "bin").glob("*.tmp")) == []


def test_native_popup_source_keeps_the_original_look():
    source = overlay_module.MACOS_SOURCE.read_text()

    for text in ("FOCUS DEVIATION", "Allow for this session", "Snooze 5 min",
                 "This reminder stays until you return to your focus zone.",
                 "NSWindowStyleMaskNonactivatingPanel", "CanJoinAllSpaces", "FullScreenAuxiliary"):
        assert text in source


def test_an_engine_with_the_popup_turns_off_the_simple_alert_window(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "_fallback_popup", True)

    FocusEngine(data_dir=tmp_path / "focus", notifier=Notifier(), overlay=FakeOverlay())

    assert notify._fallback_popup is False


def test_controller_never_raises_without_a_helper(monkeypatch):
    controller = overlay_module.OverlayController(python="/nonexistent/python")

    assert controller.show("Slack", "", "goal", ["Code"]) is False
    controller.hide()
    controller.close()
    assert controller.pids() == set()
