"""
One-command launcher for the Multimodal Workspace Analytics Platform.

Run from the project root:
    python start_project.py                 # just you, on this machine
    python start_project.py --team          # also accept team members on the network
    python start_project.py --demo          # also stream live demo members, marked Demo
    python start_project.py --behind-proxy  # members connect through an HTTPS proxy on this machine
    python start_project.py --focus         # also open the Focus Assistant
    python start_project.py --sandbox       # generated activity instead of running the collector

Starts:
    1. Activity collector (Windows, macOS, Linux)
    2. Live analytics pipeline
    3. Flask API
    4. React/Vite frontend
    5. Live demo members (with --demo)
    6. Focus Assistant (with --focus)

Press Ctrl+C to stop all services (`kill <launcher PID>` works too).

Keep this file runnable on Python 3.9, so that an old interpreter
reaches check_python_version() and gets a clear message.
"""

from pathlib import Path
import argparse
import ipaddress
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
import signal
import subprocess
import sys
import time
import webbrowser


# ============================================================
# PROJECT PATHS
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parent

COLLECTOR = PROJECT_ROOT / "collector" / "collector.py"
FOCUS_ASSISTANT = PROJECT_ROOT / "run_focus_assistant.py"
PIPELINE = PROJECT_ROOT / "live_pipeline.py"
API_SERVER = PROJECT_ROOT / "api" / "server.py"

FRONTEND_DIR = (
    PROJECT_ROOT
    / "frontend"
    / "Multimodal-Workspace-Analytics"
    / "frontend"
)

# Port 5000 is avoided because macOS AirPlay Receiver holds it.
DEFAULT_API_PORT = "5050"

# Stops a second launcher from running a second collector and pipeline
# against the same files. First line: the launcher's PID; then one
# "<PID> <name>" line per service it started, so a later launcher can
# stop services that a killed launcher left behind.
LOCK_FILE = PROJECT_ROOT / "data" / ".start_project.pid"

# File systems store modification times coarsely (FAT: 2 s), so a
# process may look up to this much younger than the lock it wrote.
LOCK_CLOCK_SLACK_SECONDS = 2.0

# Where `npm run dev` serves the dashboard: vite.config.js fixes port
# 5173 with strictPort, so this is the address the launcher waits for.
VITE_URL = "http://localhost:5173"

# --sandbox keeps generated single-user data away from your own recordings.
SANDBOX_DATA_DIR = PROJECT_ROOT / "data" / "demo"

# Values of WORKSPACE_API_HOST that mean "every interface". Without
# --team or --behind-proxy the launcher then starts the API as --team
# does, so other computers reach only the member endpoints.
ANY_ADDRESS_HOSTS = frozenset({"0.0.0.0", "::", "[::]"})

# NumPy 2.5 (requirements.txt) needs Python 3.12 or newer. Team members
# who only run collector/team_sync.py can use Python 3.9+.
MIN_PYTHON = (3, 12)

# With --team or --behind-proxy, these are the only API paths other
# computers may use: what a member's collector calls (join, ingest, me)
# and the reachability check in the README. Your own activity
# (/api/analytics.csv), pipeline status, Focus sessions and the admin
# endpoints answer only to this machine; the dashboard reaches them
# through Vite's proxy on 127.0.0.1.
MEMBER_PATHS = frozenset({
    "/api/health",
    "/api/team/join",
    "/api/team/ingest",
    "/api/team/me",
})

# Headers a reverse proxy on this machine (e.g. Caddy) sets, which the
# API trusts with --behind-proxy so it sees the visitor's address.
PROXY_HEADERS = "x-forwarded-for x-forwarded-host x-forwarded-proto"

processes = []

# True once this launcher has written the lock file.
lock_held = False


# ============================================================
# DISPLAY
# ============================================================

def say(message=""):
    """
    print() that never fails. Once the terminal is closed (SIGHUP),
    writing to it raises, and stopping the services must go on.
    """

    try:
        print(message, flush=True)
    except (OSError, ValueError):
        pass


# ============================================================
# SETTINGS
# ============================================================

def _env(name, default):
    """
    The environment variable `name`, or `default` when it is unset,
    empty or only whitespace, exactly as config._env() reads it, so
    the launcher and its services agree (`export VAR=$UNSET_VAR`
    leaves an empty value behind).
    """

    value = os.environ.get(name, "")
    return value.strip() or default


def dashboard_url():
    """
    The dashboard address the launcher opens and tells you about:
    WORKSPACE_DASHBOARD_URL, else the address Vite serves.
    """

    return _env("WORKSPACE_DASHBOARD_URL", VITE_URL)


def is_loopback_host(host):
    """
    True when an API listening on `host` is reachable from this
    machine only (127.x.x.x, ::1, localhost).
    """

    host = (host or "").strip()

    if host.lower() == "localhost":
        return True

    return is_loopback(host.strip("[]"))


def check_python_version(version=None):
    """
    Stop with a clear message on a Python the platform can't use,
    instead of letting every service fail on its imports.
    """

    version = tuple(version or sys.version_info)[:2]

    if version >= MIN_PYTHON:
        return

    needed = ".".join(str(part) for part in MIN_PYTHON)

    print()
    print(f"This is Python {version[0]}.{version[1]}; Workspace Analytics "
          f"needs Python {needed} or newer (NumPy 2.5 requires it).")
    print("On macOS, `python3` may be the system's Python 3.9. Install a newer")
    print("Python (python.org or Homebrew), then recreate the environment:")
    print(f"    python{needed} -m venv .venv          (Windows: py -{needed} -m venv .venv)")
    print("Team members who only run collector/team_sync.py can keep Python 3.9+.")
    sys.exit(1)


def print_header():
    print()
    print("=" * 70)
    print("       MULTIMODAL WORKSPACE ANALYTICS PLATFORM")
    print("=" * 70)
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Frontend     : {FRONTEND_DIR}")
    print("=" * 70)


# ============================================================
# CHECK REQUIRED FILES
# ============================================================

def check_paths():
    required = {
        "Activity Collector": COLLECTOR,
        "Live Pipeline": PIPELINE,
        "Flask API": API_SERVER,
        "Frontend Folder": FRONTEND_DIR,
    }

    missing = []

    for name, path in required.items():
        if not path.exists():
            missing.append(f"{name}: {path}")

    if missing:
        print()
        print("ERROR: Required paths were not found:")
        print()

        for item in missing:
            print(f"  - {item}")

        print()
        sys.exit(1)


# ============================================================
# START PROCESS
# ============================================================

def start_process(name, command, cwd=None):
    print(f"[STARTING] {name}...")

    creationflags = 0

    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    process = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else str(PROJECT_ROOT),
        creationflags=creationflags,
    )

    processes.append((name, process))

    if lock_held:
        # A later launcher can then stop this service if this
        # launcher is killed before it can.
        try:
            write_lock()
        except OSError as exc:
            print(f"[WARNING] Could not update {LOCK_FILE}: {exc}")

    print(f"[RUNNING]  {name} (PID {process.pid})")

    return process


# ============================================================
# STOP PROCESS
# ============================================================

def stop_process(name, process):
    if process.poll() is not None:
        return

    say(f"[STOPPING] {name}...")

    try:
        if os.name == "nt":
            # Kill the complete process tree on Windows.
            subprocess.run(
                [
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        say(f"[STOPPED]  {name}")

    except Exception as exc:
        say(f"[WARNING] Could not stop {name}: {exc}")


# ============================================================
# STOP EVERYTHING
# ============================================================

def stop_all():
    say()
    say("=" * 70)
    say("Stopping Workspace Analytics Platform...")
    say("=" * 70)

    for name, process in reversed(processes):
        stop_process(name, process)

    say()
    say("All services stopped.")
    say()


# ============================================================
# SIGNALS
# ============================================================

class StopRequested(BaseException):
    """
    Raised by the signal handlers. Like KeyboardInterrupt it is not an
    Exception, so it reaches main()'s cleanup (stop every service,
    remove the lock) and not the generic error handler.
    """


# kill <PID>, Activity Monitor's Quit or a process manager (SIGTERM),
# a closed terminal (SIGHUP), Ctrl+Break on Windows (SIGBREAK).
STOP_SIGNALS = ("SIGTERM", "SIGHUP", "SIGBREAK")


def _set_signal_handlers(handler, names):
    changed = []

    for name in names:
        signum = getattr(signal, name, None)

        if signum is None:
            continue  # not on this system

        try:
            signal.signal(signum, handler)
        except (OSError, RuntimeError, ValueError):
            continue  # e.g. not called from the main thread

        changed.append(name)

    return changed


def _raise_stop(signum, frame):
    raise StopRequested(signal.Signals(signum).name)


def install_signal_handlers():
    """
    Make SIGTERM, SIGHUP and SIGBREAK stop the launcher the way Ctrl+C
    does. By default they end it at once, without the cleanup, which
    left the collector recording and the lock file behind.
    """

    return _set_signal_handlers(_raise_stop, STOP_SIGNALS)


def ignore_stop_signals():
    """
    Called when cleanup starts: a second Ctrl+C or SIGTERM must not
    cut it short and leave services running.
    """

    return _set_signal_handlers(signal.SIG_IGN, STOP_SIGNALS + ("SIGINT",))


# ============================================================
# MAIN
# ============================================================

def parse_args(argv=None):
    return build_parser().parse_args(argv)


def build_parser():
    parser = argparse.ArgumentParser(description="Start the Workspace Analytics platform.")
    network = parser.add_mutually_exclusive_group()
    network.add_argument("--team", action="store_true",
                         help="listen on the network so team members can connect; "
                              "other computers reach only the member endpoints")
    network.add_argument("--behind-proxy", action="store_true",
                         help="for HTTPS through a reverse proxy (e.g. Caddy) on this "
                              "machine: the API stays on 127.0.0.1 without debug mode "
                              "and proxied visitors reach only the member endpoints")
    parser.add_argument("--demo", action="store_true",
                        help="also stream live demo team members into the team "
                             "server; they are marked Demo and kept out of real "
                             "members' comparisons (remove with: "
                             "python -m team.live_demo --remove)")
    parser.add_argument("--focus", action="store_true",
                        help="also open the Focus Assistant (needs Tk)")
    parser.add_argument("--sandbox", action="store_true",
                        help="show generated activity from data/demo instead of "
                             "running the collector; with --focus the Focus "
                             "Assistant still watches this computer and logs "
                             "real app names and window titles to data/demo/focus")
    parser.add_argument("--no-browser", action="store_true",
                        help="don't open the dashboard in a browser")
    return parser


def wait_for_url(url, timeout=20):
    # Not an http(s) address (e.g. "" or "localhost:5173"): waiting won't help.
    try:
        if urllib.parse.urlsplit(url or "").scheme not in ("http", "https"):
            return False
    except ValueError:
        return False

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except urllib.error.HTTPError:
            # Any HTTP answer means the server is up.
            return True
        except ValueError:
            # Not a URL at all (e.g. ""): waiting won't help.
            return False
        except OSError:
            time.sleep(0.5)

    return False


# ============================================================
# SINGLE INSTANCE
# ============================================================

def read_lock():
    """
    (launcher PID, [(service PID, name), ...], time the lock was last
    written) from the lock file, or None when there is no usable lock.
    """

    try:
        text = LOCK_FILE.read_text(encoding="utf-8")
        written = LOCK_FILE.stat().st_mtime
    except OSError:
        return None

    lines = text.splitlines()

    try:
        launcher = int(lines[0].strip())
    except (IndexError, ValueError):
        return None

    services = []

    for line in lines[1:]:
        pid, _, name = line.strip().partition(" ")

        try:
            services.append((int(pid), name.strip() or "service"))
        except ValueError:
            continue

    return launcher, services, written


def write_lock():
    lines = [str(os.getpid())]
    lines += [f"{process.pid} {name}" for name, process in processes]
    content = "\n".join(lines) + "\n"

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = LOCK_FILE.with_name(f"{LOCK_FILE.name}.{os.getpid()}.tmp")
    temp.write_text(content, encoding="utf-8")

    # Atomic, so another launcher never reads a half-written file.
    try:
        os.replace(temp, LOCK_FILE)
    except OSError:
        # Windows refuses while someone is reading the lock.
        LOCK_FILE.write_text(content, encoding="utf-8")

        try:
            temp.unlink()
        except OSError:
            pass


def _same_process(process, written, unknown):
    """
    Whether `process` can still be the one recorded in a lock file
    last written at `written`, and not a later process that got the
    same PID: it must have started before that write and belong to
    this user. `unknown` is the answer when the system won't tell.
    """

    import psutil

    checks = (
        lambda: process.create_time() <= written + LOCK_CLOCK_SLACK_SECONDS,
        lambda: process.username() == psutil.Process().username(),
        lambda: process.status() != psutil.STATUS_ZOMBIE,
    )

    certain = True

    for check in checks:
        try:
            if not check():
                return False

        # Also covers psutil.ZombieProcess: a dead process.
        except psutil.NoSuchProcess:
            return False

        except psutil.AccessDenied:
            certain = False

    return True if certain else unknown


def is_running_launcher(pid, written):
    """
    True when `pid` is a launcher that is still running. A dead PID,
    or one the system has since given to another program (Windows
    reuses PIDs quickly; after a reboot low PIDs belong to system
    processes), is a stale lock.
    """

    import psutil

    if not pid or pid == os.getpid():
        return False

    try:
        process = psutil.Process(pid)
    except psutil.Error:
        return False

    # When the system won't say, assume it is a launcher; the
    # message tells the user how to clear the lock themselves.
    if not _same_process(process, written, unknown=True):
        return False

    try:
        command = " ".join(process.cmdline())
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True

    return "start_project" in command


def leftover_services(services, written):
    """
    The recorded services that are still running after their launcher
    died without stopping them (e.g. `kill -9`), as (name, process).
    A process is only included when it is certainly the one recorded.
    """

    import psutil

    found = []

    for pid, name in services:
        if pid == os.getpid():
            continue

        try:
            process = psutil.Process(pid)
        except psutil.Error:
            continue

        if _same_process(process, written, unknown=False):
            found.append((name, process))

    return found


def stop_leftover(name, process):
    """
    Stop a service, and anything it started, left by a dead launcher.
    """

    import psutil

    say(f"[STOPPING] {name} (PID {process.pid}), left running by an earlier launcher...")

    try:
        family = [process] + process.children(recursive=True)
    except psutil.Error:
        family = [process]

    for member in family:
        try:
            member.terminate()
        except psutil.Error:
            pass

    _, alive = psutil.wait_procs(family, timeout=5)

    for member in alive:
        try:
            member.kill()
        except psutil.Error:
            pass

    psutil.wait_procs(alive, timeout=5)


def acquire_lock():
    """
    Refuse to start when another launcher is still running. A stale
    lock is taken over, after stopping any services its launcher
    left running, so there is never a second collector or pipeline
    writing the same files.
    """

    global lock_held

    lock = read_lock()

    if lock and lock[0] != os.getpid():
        other, services, written = lock

        if is_running_launcher(other, written):
            print()
            print(f"Workspace Analytics is already running (launcher PID {other}).")
            print("Stop it with Ctrl+C in its terminal first, or open "
                  f"{dashboard_url()} to use it.")
            print("If no launcher is running any more, delete the lock file "
                  "and start again:")
            print(f"    {LOCK_FILE}")
            sys.exit(1)

        leftovers = leftover_services(services, written)

        if leftovers:
            print()
            print(f"The previous launcher (PID {other}) ended without stopping "
                  "its services. Stopping them first.")

            for name, process in leftovers:
                stop_leftover(name, process)

    write_lock()
    lock_held = True


def release_lock():
    global lock_held

    lock = read_lock()

    try:
        if lock and lock[0] == os.getpid():
            LOCK_FILE.unlink()
    except OSError:
        pass

    lock_held = False


# ============================================================
# API FOR --team AND --behind-proxy
# ============================================================

def is_loopback(address):
    """
    True for 127.0.0.0/8, ::1 and ::ffff:127.x.x.x; False for anything
    else, including a missing or malformed address.
    """

    try:
        ip = ipaddress.ip_address((address or "").split("%")[0])
    except ValueError:
        return False

    mapped = getattr(ip, "ipv4_mapped", None)

    return (mapped or ip).is_loopback


class MemberEndpointsOnly:
    """
    WSGI middleware for an API reachable from other computers: they
    get MEMBER_PATHS only, everything else answers 403. Requests from
    this machine (the dashboard via Vite, the live demo) pass as is.
    """

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        if (environ.get("PATH_INFO", "") in MEMBER_PATHS
                or is_loopback(environ.get("REMOTE_ADDR"))):
            return self.app(environ, start_response)

        body = json.dumps({
            "error": "Not available from other computers. Team members "
                     "only use /api/team/join, /api/team/ingest and /api/team/me.",
        }).encode("utf-8")

        start_response("403 Forbidden", [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ])

        return [body]


def serve_api(mode="team"):
    """
    Run the API the way --team ("team") or --behind-proxy ("proxy")
    needs it; main() starts this in its own process. Unlike
    `python api/server.py` it always uses waitress (never Flask's
    debug server) and lets other computers reach MEMBER_PATHS only.

    "team" listens on WORKSPACE_API_HOST (0.0.0.0). "proxy" listens on
    127.0.0.1 only and trusts the X-Forwarded-* headers of the proxy
    there, so proxied visitors are seen with their own address and
    not as this machine.
    """

    from api.server import create_app
    from config import API_HOST, API_PORT

    try:
        from waitress import serve
    except ImportError:
        print("waitress is not installed; run: pip install -r requirements.txt",
              file=sys.stderr)
        sys.exit(1)

    app = create_app()
    app.wsgi_app = MemberEndpointsOnly(app.wsgi_app)

    if mode == "proxy":
        host = "127.0.0.1"
        options = {"trusted_proxy": "127.0.0.1", "trusted_proxy_headers": PROXY_HEADERS}
    else:
        host = API_HOST
        options = {}

    print("=" * 60)
    print("Workspace Analytics API (waitress, debug mode off)")
    print("=" * 60)
    print(f"API:        http://{host}:{API_PORT}")

    if mode == "proxy":
        print("Waiting for a reverse proxy on this machine (see the README,")
        print("'HTTPS for team mode'). Proxied visitors can only use:")
    else:
        print("Other computers can only use:")

    print("  " + ", ".join(sorted(MEMBER_PATHS)))
    print("Your own activity, Focus sessions, status and the admin")
    print("endpoints answer only to this machine.")

    if mode != "proxy":
        print("Members' data travels over plain HTTP. Use a trusted")
        print("network, or put the server behind HTTPS.")

    print()
    print("Team admin key (paste into the Team page):")
    print(app.config["TEAM_ADMIN_KEY"])
    print("=" * 60, flush=True)

    serve(app, host=host, port=API_PORT, **options)


def api_command(args):
    """
    The command that starts the API: `python api/server.py` for one
    user on this machine, serve_api() when other computers connect.
    """

    if args.team or args.behind_proxy:
        mode = "proxy" if args.behind_proxy else "team"
        return [
            sys.executable,
            "-c",
            f"import start_project; start_project.serve_api({mode!r})",
        ]

    return [sys.executable, str(API_SERVER)]


# ============================================================
# FRONTEND DEPENDENCIES
# ============================================================

def _read_lock_packages(path):
    """
    The "packages" table of an npm lock file, or None.
    """

    try:
        packages = json.loads(path.read_text(encoding="utf-8")).get("packages")
    except (OSError, ValueError, AttributeError):
        return None

    return packages if isinstance(packages, dict) else None


def frontend_packages_installed():
    """
    True when node_modules holds a finished install of package-lock.json.

    npm writes node_modules/.package-lock.json only at the end of an
    install, so an interrupted first `npm ci` (which empties
    node_modules before filling it) leaves none, and a package-lock.json
    changed by `git pull` asks for versions that are not installed.
    Packages for other platforms are optional and never installed.
    """

    modules = FRONTEND_DIR / "node_modules"

    if not any((modules / ".bin" / name).exists() for name in ("vite", "vite.cmd")):
        return False

    installed = _read_lock_packages(modules / ".package-lock.json")
    wanted = _read_lock_packages(FRONTEND_DIR / "package-lock.json")

    if not installed or not wanted:
        return False

    for path, entry in wanted.items():
        # "" is the dashboard itself, not an installed package.
        if not path or not isinstance(entry, dict):
            continue

        have = installed.get(path)

        if have is None:
            if entry.get("optional") or entry.get("devOptional"):
                continue
            return False

        if not isinstance(have, dict) or have.get("version") != entry.get("version"):
            return False

    return True


def ensure_frontend_ready(npm_command):
    """
    Return True when `npm run dev` can work, installing the
    dashboard's packages on first run, and again whenever the
    install is incomplete or older than package-lock.json.
    """

    if shutil.which(npm_command) is None:
        print()
        print("[WARNING] npm was not found, so the dashboard can't start.")
        print("          Install Node.js 22 LTS from https://nodejs.org and run this again.")
        return False

    if frontend_packages_installed():
        return True

    if (FRONTEND_DIR / "node_modules").exists():
        print("[SETUP]    Dashboard packages are incomplete or out of date; "
              "reinstalling them (npm ci)...")
    else:
        print("[SETUP]    Installing dashboard packages (npm ci, first run only)...")

    result = subprocess.run([npm_command, "ci"], cwd=str(FRONTEND_DIR), check=False)

    if result.returncode != 0:
        print("[WARNING] npm ci failed; see the messages above. The next start")
        print(f"          tries again, or run `npm ci` yourself in {FRONTEND_DIR}")
        return False

    return True


def api_port_setting():
    """
    WORKSPACE_API_PORT as the services will read it, or the default.
    Stops with a clear message on a value that is not a port number.
    """

    port = _env("WORKSPACE_API_PORT", DEFAULT_API_PORT)

    if not port.isdigit() or not 0 < int(port) < 65536:
        print()
        print(f"WORKSPACE_API_PORT must be a port number such as {DEFAULT_API_PORT}, "
              f"not {port!r}.")
        print("Fix it, or unset it to use the default.")
        sys.exit(1)

    return str(int(port))


def network_host_requested(host):
    """
    Whether a WORKSPACE_API_HOST left in the environment asks for the
    network. `python api/server.py` would serve every endpoint there,
    your activity log included, so every interface (0.0.0.0) starts the
    API as --team does: other computers reach only the member
    endpoints. A single non-loopback address is refused: the dashboard
    and the launcher reach the API on 127.0.0.1, so it could not work.
    """

    if is_loopback_host(host):
        return False

    if host in ANY_ADDRESS_HOSTS:
        print()
        print(f"WORKSPACE_API_HOST={host} asks for the network, so the API starts")
        print("as with --team: other computers reach only the member endpoints.")
        return True

    print()
    print(f"WORKSPACE_API_HOST is set to {host!r}. The launcher keeps the API on")
    print("this machine, or on every interface with --team:")
    print("    python start_project.py --team")
    print("Unset WORKSPACE_API_HOST (PowerShell: Remove-Item Env:WORKSPACE_API_HOST)")
    print("and start again.")
    sys.exit(1)


def main(argv=None):

    check_python_version()

    args = parse_args(argv)

    # Child processes inherit these, so the API, the Vite proxy and the
    # demo all agree on the host and port. A blank value counts as
    # unset, as in config.py.
    os.environ["WORKSPACE_API_PORT"] = api_port_setting()

    if not (args.team or args.behind_proxy):
        args.team = network_host_requested(_env("WORKSPACE_API_HOST", "127.0.0.1"))

    if args.team:
        os.environ["WORKSPACE_API_HOST"] = "0.0.0.0"

    if args.behind_proxy:
        # The reverse proxy on this machine is the only way in.
        os.environ["WORKSPACE_API_HOST"] = "127.0.0.1"

    # Every child process reads WORKSPACE_DATA_DIR through config.py, so
    # the feed, pipeline, API and Focus Assistant all use data/demo.
    if args.sandbox:
        os.environ["WORKSPACE_DATA_DIR"] = str(SANDBOX_DATA_DIR)

    # --demo streams into the team database the server already uses, so
    # members who joined keep uploading. Demo members are created as
    # simulated: marked Demo, analysed apart from real members, and
    # removed with `python -m team.live_demo --remove`.

    api_url = f"http://127.0.0.1:{os.environ['WORKSPACE_API_PORT']}"
    dashboard = dashboard_url()

    print_header()

    # --------------------------------------------------------
    # Check project structure
    # --------------------------------------------------------

    check_paths()
    install_signal_handlers()
    acquire_lock()

    dashboard_running = False

    try:

        # ----------------------------------------------------
        # 1. Activity Collector
        # ----------------------------------------------------

        if args.sandbox:
            start_process(
                "Sandbox Activity Feed",
                [
                    sys.executable,
                    "-m",
                    "collector.demo_feed",
                    "--backfill-hours",
                    "8",
                ],
                cwd=PROJECT_ROOT,
            )
        else:
            start_process(
                "Activity Collector",
                [
                    sys.executable,
                    str(COLLECTOR),
                ],
                cwd=PROJECT_ROOT,
            )

        time.sleep(1)

        # ----------------------------------------------------
        # 2. Live Analytics Pipeline
        # ----------------------------------------------------

        start_process(
            "Live Analytics Pipeline",
            [
                sys.executable,
                str(PIPELINE),
            ],
            cwd=PROJECT_ROOT,
        )

        time.sleep(1)

        # ----------------------------------------------------
        # 3. Flask API
        # ----------------------------------------------------

        start_process(
            "Flask API",
            api_command(args),
            cwd=PROJECT_ROOT,
        )

        if not wait_for_url(f"{api_url}/api/health"):
            print(f"[WARNING] Flask API did not answer at {api_url} yet.")

        # ----------------------------------------------------
        # 4. React / Vite Frontend
        # ----------------------------------------------------

        npm_command = "npm.cmd" if os.name == "nt" else "npm"

        if ensure_frontend_ready(npm_command):
            start_process(
                "React Dashboard",
                [
                    npm_command,
                    "run",
                    "dev",
                ],
                cwd=FRONTEND_DIR,
            )
            dashboard_running = True

        # ----------------------------------------------------
        # 5. Live demo members (optional)
        # ----------------------------------------------------

        if args.demo:
            start_process(
                "Live Demo Members",
                [
                    sys.executable,
                    "-m",
                    "team.live_demo",
                    "--server",
                    api_url,
                ],
                cwd=PROJECT_ROOT,
            )

        # ----------------------------------------------------
        # 6. Focus Assistant (optional)
        # ----------------------------------------------------

        if args.focus:
            start_process(
                "Focus Assistant",
                [
                    sys.executable,
                    str(FOCUS_ASSISTANT),
                ],
                cwd=PROJECT_ROOT,
            )

        # ----------------------------------------------------
        # Everything started
        # ----------------------------------------------------

        print()
        print("=" * 70)
        print("ALL SERVICES STARTED")
        print("=" * 70)
        print()
        if args.sandbox:
            print(f"Activity source    : generated (sandbox data in {SANDBOX_DATA_DIR})")
        else:
            print("Activity Collector : RUNNING")
        print("Live Pipeline      : RUNNING")
        print(f"Flask API          : {api_url}")
        print(f"React Dashboard    : {dashboard if dashboard_running else 'NOT STARTED'}")
        if args.team:
            print("Team mode          : ON (members join with the command on the Team page;")
            print("                     other computers reach only the member endpoints)")
        if args.behind_proxy:
            print(f"Reverse proxy      : point it at {api_url} (members' endpoints only)")
        if args.demo:
            print("Demo members       : streaming, marked Demo and kept out of real")
            print("                     members' comparisons and totals")
            print("                     (python -m team.live_demo --remove deletes them)")
        if args.focus:
            print("Focus Assistant    : opening in its own window")
        print()
        print("=" * 70)
        if dashboard_running and not args.no_browser:
            print("Opening dashboard in your browser...")
        if os.name == "nt":
            print("Press Ctrl+C in this terminal to stop everything.")
        else:
            print("Press Ctrl+C in this terminal to stop everything "
                  f"(or: kill {os.getpid()}).")
        print("=" * 70)
        print()

        if dashboard_running and not args.no_browser:
            # Wait for the Vite server this launcher started, then open
            # the address you asked for.
            if wait_for_url(VITE_URL, timeout=30):
                webbrowser.open(dashboard)
            else:
                print(f"[WARNING] The dashboard did not answer at {VITE_URL}.")

        # ----------------------------------------------------
        # Keep launcher alive
        # ----------------------------------------------------

        reported = set()

        while True:

            time.sleep(2)

            # Report each service that stops unexpectedly, once.
            for name, process in processes:

                exit_code = process.poll()

                if exit_code is not None and name not in reported:

                    reported.add(name)
                    print()
                    print(
                        f"[WARNING] {name} stopped "
                        f"with exit code {exit_code}. "
                        "The other services keep running."
                    )

    except KeyboardInterrupt:

        say()
        say("Ctrl+C received.")

    except StopRequested as stop:

        say()
        say(f"{stop} received.")

    except Exception as exc:

        say()
        say(f"[ERROR] Launcher failed: {exc}")

    finally:

        ignore_stop_signals()

        try:
            stop_all()
        finally:
            release_lock()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()