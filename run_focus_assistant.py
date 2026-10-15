import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


DESCRIPTION = (
    "Open the Focus Assistant: a desktop window (Tk) for timed focus sessions. "
    "Declare the applications a session is for, and it reminds you when "
    "another application comes to the front. Sessions are saved in "
    "data/focus (or $WORKSPACE_DATA_DIR/focus), where the dashboard's Focus "
    "page reads them. Only one assistant runs per data directory."
)


def tk_install_hint():
    """How to add Tk to the Python running this script, per OS."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return "\n".join([
        "The Focus Assistant needs Tk (tkinter), and this Python does not include it:",
        f"  {sys.executable}",
        "",
        "Install Tk for this Python, then run the command again:",
        f"  macOS (Homebrew Python):  brew install python-tk@{version}",
        "  macOS / Windows (python.org installer): run the installer again, choose",
        "      Modify, and tick 'tcl/tk and IDLE'",
        "  Debian / Ubuntu:          sudo apt install python3-tk",
        "  Fedora:                   sudo dnf install python3-tkinter",
    ])


def build_parser():
    return argparse.ArgumentParser(
        prog="run_focus_assistant.py",
        description=DESCRIPTION,
    )


def main(argv=None):
    # Parsed before anything else, so --help never opens a window.
    build_parser().parse_args(argv)

    try:
        import tkinter  # noqa: F401
    except ImportError:
        # e.g. ModuleNotFoundError: No module named '_tkinter'
        print(tk_install_hint(), file=sys.stderr)
        return 1

    from focus.focus_gui import launch_focus_assistant

    return launch_focus_assistant()


if __name__ == "__main__":
    sys.exit(main())
