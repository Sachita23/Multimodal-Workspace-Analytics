"""
The single-user collector: raw file handling, app names, idle
time and sample scheduling. Nothing here touches data/raw or the
real desktop; every OS call is monkeypatched.
"""

import csv
import ctypes
import json
import os
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from collector import collector, team_sync
from config import RAW_COLUMNS, TEAM_CONSENT_VERSION


def row(timestamp="2026-10-15 09:00:00", **overrides):
    return {
        "timestamp": timestamp,
        "application": "Visual Studio Code",
        "window_title": "main.py - VS Code",
        "activity_status": "Active",
        "idle_seconds": 1.0,
        "cpu_percent": 20.0,
        "memory_percent": 50.0,
        "battery_percent": 90.0,
        "charging": True,
        **overrides,
    }


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as file:
        return list(csv.reader(file))


@pytest.fixture
def raw_file(tmp_path, monkeypatch):
    path = tmp_path / "raw" / "workspace_activity.csv"
    path.parent.mkdir()
    monkeypatch.setattr(collector, "RAW_DATA_FILE", path)
    monkeypatch.setattr(collector, "_prepared_files", set())
    return path


# Root ignores file permissions, and Windows has no POSIX bits.
needs_posix_permissions = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX file permissions enforced",
)


# --------------------------------------------------
# Raw CSV
# --------------------------------------------------

def test_save_data_writes_header_into_empty_existing_file(raw_file):
    raw_file.touch()

    collector.save_data(row("2026-10-15 09:00:00"))
    collector.save_data(row("2026-10-15 09:00:05"))

    rows = read_rows(raw_file)
    assert rows[0] == RAW_COLUMNS
    assert [r[0] for r in rows[1:]] == ["2026-10-15 09:00:00", "2026-10-15 09:00:05"]


def test_save_data_creates_missing_file_with_header(raw_file):
    collector.save_data(row())

    rows = read_rows(raw_file)
    assert rows[0] == RAW_COLUMNS
    assert len(rows) == 2


def test_old_header_is_rotated_aside(raw_file, capsys):
    old_columns = [c for c in RAW_COLUMNS if c != "window_title"]
    raw_file.write_text(
        ",".join(old_columns) + "\n"
        "2026-09-01 09:00:00,Slack,Active,1,2,3,4,False\n",
        encoding="utf-8",
    )

    collector.save_data(row())

    rotated = list(raw_file.parent.glob("workspace_activity.*.csv"))
    assert len(rotated) == 1
    today = datetime.now().strftime("%Y-%m-%d")
    assert rotated[0].name == f"workspace_activity.{today}.csv"
    assert read_rows(rotated[0])[0] == old_columns

    rows = read_rows(raw_file)
    assert rows[0] == RAW_COLUMNS
    assert len(rows) == 2
    assert "moved it to" in capsys.readouterr().out


def test_file_without_header_is_rotated_not_appended_to(raw_file):
    # What the old collector produced from a 0-byte file: data as header.
    raw_file.write_text(
        "2026-10-01 09:00:00,Slack,Unknown,Active,1,2,3,4,False\n",
        encoding="utf-8",
    )

    collector.prepare_raw_file(raw_file)

    assert not raw_file.exists()
    assert len(list(raw_file.parent.glob("workspace_activity.*.csv"))) == 1


def test_rotation_never_overwrites_an_earlier_rotation(raw_file):
    today = datetime.now().strftime("%Y-%m-%d")

    for _ in range(2):
        raw_file.write_text("a,b\n1,2\n", encoding="utf-8")
        collector.prepare_raw_file(raw_file)

    names = sorted(p.name for p in raw_file.parent.glob("workspace_activity.*.csv"))
    assert names == [
        f"workspace_activity.{today}-2.csv",
        f"workspace_activity.{today}.csv",
    ]


HEADER = ",".join(RAW_COLUMNS) + "\r\n"
COMPLETE_ROW = "2026-10-15 09:00:00,Slack,general,Active,1,2,3,4,False\r\n"


@pytest.mark.parametrize("cut_row", [
    "2026-10-15 09:00:05,Sla",
    # Cut inside a quoted title: an open quote would swallow every
    # later row and pandas would fail on the whole file.
    '2026-10-15 09:00:05,Microsoft Outlook,"Re: budget, Q3',
    # ...even when that title has a newline in it.
    '2026-10-15 09:00:05,Notepad,"line one\r\nline t',
    "2026-10-15 09:00:05,Slack,general,Active,1,2,3,4,False\r",
])
def test_matching_file_is_kept_and_cut_off_row_is_dropped(raw_file, cut_row, capsys):
    pandas = pytest.importorskip("pandas")
    raw_file.write_bytes((HEADER + COMPLETE_ROW + cut_row).encode("utf-8"))

    collector.save_data(row("2026-10-15 09:00:10", window_title='Re: "x", y\nz'))

    assert list(raw_file.parent.glob("workspace_activity.*.csv")) == []
    rows = read_rows(raw_file)
    assert rows[0] == RAW_COLUMNS
    assert [r[0] for r in rows[1:]] == ["2026-10-15 09:00:00", "2026-10-15 09:00:10"]
    assert all(len(r) == len(RAW_COLUMNS) for r in rows)
    assert "dropped an unfinished last row" in capsys.readouterr().out

    frame = pandas.read_csv(raw_file)
    assert list(frame["timestamp"]) == ["2026-10-15 09:00:00", "2026-10-15 09:00:10"]


def test_complete_quoted_multiline_row_is_kept(raw_file):
    complete = '2026-10-15 09:00:05,Notepad,"a\r\nb ""c""",Active,1,2,3,4,False\r\n'
    raw_file.write_bytes((HEADER + complete + "2026-10-15 09:00:1").encode("utf-8"))

    collector.prepare_raw_file(raw_file)

    assert raw_file.read_bytes() == (HEADER + complete).encode("utf-8")


def test_file_holding_only_a_header_without_newline_starts_over(raw_file):
    raw_file.write_bytes(",".join(RAW_COLUMNS).encode("utf-8"))

    collector.save_data(row())

    rows = read_rows(raw_file)
    assert rows[0] == RAW_COLUMNS
    assert len(rows) == 2


@needs_posix_permissions
def test_read_only_raw_file_that_needs_no_repair_is_only_read(raw_file):
    raw_file.write_text(HEADER + COMPLETE_ROW, encoding="utf-8", newline="")
    os.chmod(raw_file, 0o444)

    try:
        assert collector.prepare_raw_file(raw_file) is None
    finally:
        os.chmod(raw_file, 0o644)


@needs_posix_permissions
def test_unreadable_raw_file_is_not_mistaken_for_a_wrong_header(raw_file):
    raw_file.write_text(HEADER + COMPLETE_ROW, encoding="utf-8", newline="")
    os.chmod(raw_file, 0)

    try:
        with pytest.raises(PermissionError):
            collector.prepare_raw_file(raw_file)
    finally:
        os.chmod(raw_file, 0o644)

    assert list(raw_file.parent.glob("workspace_activity.*.csv")) == []


@needs_posix_permissions
def test_locked_raw_file_does_not_stop_the_collector(raw_file, monkeypatch, capsys):
    # chmod 444 stands in for Excel holding the file on Windows.
    raw_file.write_text(HEADER + COMPLETE_ROW + "2026-10-15 09:00:05,Sla",
                        encoding="utf-8", newline="")
    os.chmod(raw_file, 0o444)

    samples = []

    def collect():
        samples.append(row("2026-10-15 09:00:%02d" % (10 + 5 * len(samples))))
        if len(samples) == 2:
            os.chmod(raw_file, 0o644)       # Excel closed
        if len(samples) > 3:
            raise KeyboardInterrupt
        return samples[-1]

    monkeypatch.setattr(collector, "collect_data", collect)
    monkeypatch.setattr(collector, "COLLECTION_INTERVAL", 0.01)

    try:
        collector.main([])
    finally:
        os.chmod(raw_file, 0o644)

    out = capsys.readouterr().out
    assert "Warning:" in out
    assert "Collector stopped." in out
    assert "Error saving data" in out           # the first, locked sample

    # Nothing was appended to the file while it could not be fixed;
    # once it could, the cut row was dropped and recording resumed.
    rows = read_rows(raw_file)
    assert [r[0] for r in rows[1:]] == [
        "2026-10-15 09:00:00", "2026-10-15 09:00:15", "2026-10-15 09:00:20",
    ]


# --------------------------------------------------
# Application names
# --------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("WhatsApp", "WhatsApp"),
    ("Microsoft PowerPoint", "Microsoft PowerPoint"),
    ("TV", "TV"),
    ("Find My", "Find My"),
    ("whatsapp.exe", "WhatsApp"),
    ("POWERPNT.EXE", "Microsoft PowerPoint"),
    ("Notion.exe", "Notion"),
    ("notion.exe", "Notion"),
    ("gnome-terminal", "Gnome-Terminal"),
    ("Code", "Visual Studio Code"),
    ("code.exe", "Visual Studio Code"),
    ("zoom.us", "Zoom"),
    ("iTerm2", "iTerm2"),
    # Windows executables shipped in capitals keep their old names...
    ("WINPROJ.EXE", "Winproj"),
    ("ONENOTE.EXE", "Microsoft OneNote"),
    ("MSACCESS.EXE", "Microsoft Access"),
    ("MSPUB.EXE", "Microsoft Publisher"),
    ("VISIO.EXE", "Microsoft Visio"),
    ("olk.exe", "Microsoft Outlook"),
    # ...mixed-case ones keep their casing.
    ("SnippingTool.exe", "SnippingTool"),
    ("AcroRd32.exe", "AcroRd32"),
    ("Firefox", "Mozilla Firefox"),
    ("loginwindow", "Locked"),
    ("ScreenSaverEngine", "Locked"),
    ("LockApp.exe", "Locked"),
    ("", "Unknown"),
    (None, "Unknown"),
])
def test_clean_application_name(raw, expected):
    assert collector.clean_application_name(raw) == expected


def test_locked_screen_is_always_idle(monkeypatch):
    monkeypatch.setattr(
        collector, "get_active_process", lambda: ("Locked", "Unknown", 88)
    )
    monkeypatch.setattr(collector, "get_idle_time", lambda: 2.0)
    monkeypatch.setattr(
        collector, "get_system_metrics", lambda: (1.0, 2.0, 3.0, False)
    )

    data = collector.collect_data()

    assert data["application"] == "Locked"
    assert data["activity_status"] == "Idle"
    assert list(data) == RAW_COLUMNS


# --------------------------------------------------
# Active window and process id
# --------------------------------------------------

@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(collector, "IS_WINDOWS", False)
    monkeypatch.setattr(collector, "IS_MAC", True)
    monkeypatch.setattr(collector, "_hints_shown", set())

    commands = []

    def answer(output):
        def fake_run(command, timeout=2):
            commands.append(command)
            return output
        monkeypatch.setattr(collector, "_run", fake_run)

    return SimpleNamespace(answer=answer, commands=commands)


def test_mac_reads_app_title_and_pid_in_one_call(mac, capsys):
    mac.answer("Code\t4242\tcollector.py — Multimodal")

    assert collector.get_active_process() == (
        "Visual Studio Code", "collector.py — Multimodal", 4242
    )
    assert collector.get_active_application() == (
        "Visual Studio Code", "collector.py — Multimodal"
    )
    assert len(mac.commands) == 2           # one osascript per call
    assert all(c[0] == "osascript" for c in mac.commands)
    assert capsys.readouterr().out == ""


def test_mac_title_with_tab_is_kept_whole(mac):
    mac.answer("Safari\t77\tA\tB")

    assert collector.get_active_process() == ("Safari", "A\tB", 77)


def test_mac_missing_title_prints_accessibility_hint_once(mac, capsys):
    mac.answer("Slack\t901")

    assert collector.get_active_process() == ("Slack", "Unknown", 901)
    collector.get_active_process()

    out = capsys.readouterr().out
    assert out.count("Accessibility") == 1


def test_mac_osascript_failure_reports_unknown(mac, capsys):
    mac.answer("")

    assert collector.get_active_process() == ("Unknown", "Unknown", None)
    assert "Automation" in capsys.readouterr().out


def test_errors_report_unknown_without_pid(monkeypatch):
    monkeypatch.setattr(collector, "IS_WINDOWS", False)
    monkeypatch.setattr(collector, "IS_MAC", False)

    def broken():
        raise RuntimeError("no display")

    monkeypatch.setattr(collector, "_active_window_linux", broken)

    assert collector.get_active_process() == ("Unknown", "Unknown", None)
    assert collector.get_active_application() == ("Unknown", "Unknown")


def test_title_cut_inside_an_emoji_is_made_valid(raw_file, monkeypatch):
    # pywin32 hands back GetWindowTextW's UTF-16 as is, so a title cut
    # in the middle of an emoji holds a lone surrogate.
    monkeypatch.setattr(collector, "IS_WINDOWS", True)
    monkeypatch.setattr(
        collector, "_active_window_windows",
        lambda: ("chrome.exe", "Chat with Ana \ud83d", 12),
    )
    monkeypatch.setattr(collector, "get_idle_time", lambda: 2.0)
    monkeypatch.setattr(
        collector, "get_system_metrics", lambda: (1.0, 2.0, 3.0, False)
    )

    assert collector.get_active_process() == (
        "Google Chrome", "Chat with Ana �", 12
    )

    collector.save_data(collector.collect_data())

    assert read_rows(raw_file)[1][2] == "Chat with Ana �"


@pytest.mark.parametrize("raw, expected", [
    ("plain", "plain"),
    ("\U0001F600 kept", "\U0001F600 kept"),
    ("a\udc80b", "a�b"),
    ("😀", "\U0001F600"),     # a split pair is joined again
    (None, None),
])
def test_clean_text(raw, expected):
    assert collector.clean_text(raw) == expected


# --------------------------------------------------
# Windows idle time
# --------------------------------------------------

def test_tick_difference_survives_signed_overflow():
    # 25 days of uptime, 10 minutes since the last input. Read as a
    # signed int GetTickCount gives -2134967296, and the old
    # max(0, tick - dwTime) reported 0 s.
    tick = -2134967296 & 0xFFFFFFFF
    last_input = 2159400000

    assert collector._elapsed_ticks_ms(tick, last_input) == 600_000
    assert collector._elapsed_ticks_ms(-2134967296, last_input) == 600_000


def test_tick_difference_across_the_49_day_wrap():
    assert collector._elapsed_ticks_ms(1_000, 0xFFFFFFFF - 999) == 2_000


def test_last_input_slightly_after_the_tick_read_is_no_idle_time():
    # Input injected with its own timestamp (remote desktop) can be
    # stamped after GetTickCount was read; that is not 49.7 days.
    assert collector._elapsed_ticks_ms(5_000_000, 5_000_016) == 0
    # ...also when that happens across the wrap.
    assert collector._elapsed_ticks_ms(0xFFFFFFFF - 5, 10) == 0


def test_windows_idle_time_with_input_stamped_ahead(monkeypatch):
    def get_last_input_info(info_ref):
        info_ref._obj.dwTime = 5_000_016
        return 1

    windll = SimpleNamespace(
        user32=SimpleNamespace(GetLastInputInfo=FakeWinFunction(get_last_input_info)),
        kernel32=SimpleNamespace(GetTickCount=FakeWinFunction(lambda: 5_000_000)),
    )
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)

    idle = collector._idle_time_windows()

    assert idle == 0.0
    assert collector.get_activity_status(idle, "Google Chrome") == "Active"


class FakeWinFunction:
    def __init__(self, function):
        self.function = function
        self.restype = None

    def __call__(self, *args):
        return self.function(*args)


def test_windows_idle_time_uses_unsigned_ticks(monkeypatch):
    def get_last_input_info(info_ref):
        info_ref._obj.dwTime = 2159400000
        return 1

    windll = SimpleNamespace(
        user32=SimpleNamespace(GetLastInputInfo=FakeWinFunction(get_last_input_info)),
        kernel32=SimpleNamespace(GetTickCount=FakeWinFunction(lambda: -2134967296)),
    )
    monkeypatch.setattr(ctypes, "windll", windll, raising=False)

    assert collector._idle_time_windows() == pytest.approx(600.0)
    assert windll.kernel32.GetTickCount.restype is ctypes.c_uint32


# --------------------------------------------------
# Sample scheduling
# --------------------------------------------------

def test_next_deadline_keeps_a_fixed_grid():
    # Work took 1.2 s: the next sample is still 5 s after the last.
    assert collector.next_deadline(100.0, 5, now=101.2) == 105.0


def test_next_deadline_does_not_burst_after_falling_behind():
    # The laptop slept for an hour: one sample now, not 720.
    assert collector.next_deadline(100.0, 5, now=3700.0) == 3700.0


@pytest.fixture
def joined(tmp_path, monkeypatch):
    """
    A joined team_sync with fake collection, fast intervals and its
    state in tmp_path.
    """

    monkeypatch.setattr(team_sync, "STATE_DIR", tmp_path)
    monkeypatch.setattr(team_sync, "CONFIG_FILE", tmp_path / "team.json")
    monkeypatch.setattr(team_sync, "QUEUE_FILE", tmp_path / "queue.db")
    team_sync.save_config({
        "server": "http://127.0.0.1:5050",
        "token": "token",
        "member_id": 1,
        "name": "Asha",
        "consent_version": TEAM_CONSENT_VERSION,
    })

    samples = []
    start = datetime.now() - timedelta(minutes=5)

    def fake_collect(count=12):
        samples.append(time.monotonic())
        if len(samples) > count:
            raise KeyboardInterrupt
        return row((start + timedelta(seconds=len(samples))).strftime("%Y-%m-%d %H:%M:%S"))

    monkeypatch.setattr(team_sync, "collect_data", fake_collect)
    return samples


def test_sampling_cadence_holds_while_uploads_are_slow(joined, monkeypatch):
    interval = 0.2
    monkeypatch.setattr(team_sync, "COLLECTION_INTERVAL", interval)
    monkeypatch.setattr(team_sync, "TEAM_SYNC_INTERVAL", 0)

    uploads = []

    def slow_request(method, url, body=None, token=None, timeout=15):
        time.sleep(0.12)
        uploads.append(len(body["observations"]))
        return {"accepted": len(body["observations"]), "duplicates": 0, "rejected": 0}

    monkeypatch.setattr(team_sync, "request_json", slow_request)

    assert team_sync.main(["run", "--quiet"]) == 0

    samples = joined[:-1]
    elapsed = samples[-1] - samples[0]
    expected = interval * (len(samples) - 1)

    # Sleeping a fixed interval after each slow upload would take
    # (0.2 + 0.12) per sample, i.e. about 3.5 s instead of 2.2 s. The
    # bound sits halfway between the two, so a slow CI machine's
    # scheduling jitter does not fail the test but that bug still does.
    drifting = (interval + 0.12) * (len(samples) - 1)
    assert expected * 0.9 <= elapsed < (expected + drifting) / 2
    assert len(uploads) >= len(samples) - 1


def test_unreachable_server_is_retried_with_backoff_not_every_sample(joined, monkeypatch):
    monkeypatch.setattr(team_sync, "COLLECTION_INTERVAL", 0.05)
    monkeypatch.setattr(team_sync, "TEAM_SYNC_INTERVAL", 0)

    attempts = []

    def unreachable(method, url, body=None, token=None, timeout=15):
        attempts.append(time.monotonic())
        raise OSError("timed out")

    monkeypatch.setattr(team_sync, "request_json", unreachable)

    assert team_sync.main(["run", "--quiet"]) == 0

    # One attempt while sampling (then a 30 s backoff), one on Ctrl+C.
    assert len(attempts) == 2

    conn = team_sync.open_queue()
    assert team_sync.queued_count(conn) == len(joined) - 1
    conn.close()


def test_a_large_backlog_is_sent_a_few_batches_per_sample(joined, monkeypatch):
    conn = team_sync.open_queue()
    for _ in range(25):
        team_sync.enqueue(conn, row())
    conn.close()

    batches = []

    def server(method, url, body=None, token=None, timeout=15):
        batches.append(len(body["observations"]))
        return {"accepted": len(body["observations"]), "duplicates": 0, "rejected": 0}

    monkeypatch.setattr(team_sync, "request_json", server)
    state = team_sync.SyncState()
    state.batch_size = 2

    conn = team_sync.open_queue()
    assert team_sync.flush(conn, team_sync.load_config(), state,
                           team_sync.MAX_BATCHES_PER_SYNC) is True

    assert batches == [2] * team_sync.MAX_BATCHES_PER_SYNC
    assert team_sync.queued_count(conn) == 25 - 2 * team_sync.MAX_BATCHES_PER_SYNC
    # More is waiting, so the next sample syncs again straight away.
    assert state.next_sync <= time.monotonic()
    conn.close()


def test_queue_payload_matches_raw_columns(joined):
    conn = team_sync.open_queue()
    team_sync.enqueue(conn, row())

    payload = json.loads(conn.execute("SELECT payload FROM queue").fetchone()[0])
    assert list(payload) == RAW_COLUMNS
    conn.close()
