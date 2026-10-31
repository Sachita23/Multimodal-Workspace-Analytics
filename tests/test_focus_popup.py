"""
The deviation popup: a window that stays until dismissed, one at a time,
with the texts passed as arguments (never as script source).
"""

import subprocess

import pytest

from focus import analytics, notify


HOSTILE = 'Slack"; do shell script "rm -rf ~" --'


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_popup_command_passes_texts_as_data(platform, monkeypatch):
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")

    argv, env = notify.popup_command("Focus deviation", f"You switched to {HOSTILE}", platform)
    script = " ".join(argv[:-2]) if platform == "darwin" else " ".join(argv)

    if platform == "darwin":
        assert argv[-2:] == ["Focus deviation", f"You switched to {HOSTILE}"]
        assert "rm -rf" not in script and "giving up after" in script
    elif platform == "win32":
        assert "rm -rf" not in script
        assert env["WORKSPACE_NOTIFY_MESSAGE"].endswith(HOSTILE)
        assert notify.POPUP_WINDOW_TITLE in script
    else:
        assert argv[0].endswith("zenity") and "--no-markup" in argv


def test_only_one_popup_at_a_time(monkeypatch):
    started = []

    class FakeProcess:
        def __init__(self):
            self.done = False

        def poll(self):
            return 0 if self.done else None

    def fake_spawn(argv, env):
        process = FakeProcess()
        started.append(process)
        return process

    monkeypatch.setattr(notify, "_popup_process", None)
    monkeypatch.setattr(notify, "_spawn", fake_spawn)
    monkeypatch.setattr(notify, "popup_command", lambda title, message: (["popup"], {}))
    monkeypatch.delenv(notify.DISABLE_ENV, raising=False)
    monkeypatch.delenv(notify.POPUP_ENV, raising=False)

    assert notify.popup("Focus deviation", "one") is True
    assert notify.popup("Focus deviation", "two") is False   # first still open
    started[0].done = True
    assert notify.popup("Focus deviation", "three") is True
    assert len(started) == 2


def test_popup_can_be_turned_off(monkeypatch):
    monkeypatch.setenv(notify.POPUP_ENV, "0")
    monkeypatch.setattr(notify, "_spawn", lambda *a: pytest.fail("popup opened"))

    assert notify.popup("Focus deviation", "x") is False


def test_popup_never_raises(monkeypatch):
    def boom(*args):
        raise OSError("no helper")

    monkeypatch.setattr(notify, "_popup_process", None)
    monkeypatch.setattr(notify, "_spawn", boom)
    monkeypatch.setattr(notify, "popup_command", lambda title, message: (["popup"], {}))
    monkeypatch.delenv(notify.POPUP_ENV, raising=False)
    monkeypatch.delenv(notify.DISABLE_ENV, raising=False)

    assert notify.popup("Focus deviation", "x") is False


def test_looking_at_the_popup_is_not_a_deviation():
    # macOS: the alert belongs to osascript; Windows: its title marks it.
    assert analytics.is_neutral_window("Osascript", "")
    assert analytics.has_internal_title("Focus deviation")
