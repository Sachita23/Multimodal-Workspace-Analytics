import logging
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from collector.collector import get_active_application, get_idle_time
try:
    # (application, window_title, pid): lets the assistant recognise its
    # own windows by process id instead of by title.
    from collector.collector import get_active_process
except ImportError:
    get_active_process = None
from focus import analytics
from focus.focus_manager import FocusManager, InstanceLock

LOGGER = logging.getLogger(__name__)

DASHBOARD_URL = str(getattr(config, "DASHBOARD_URL", "http://localhost:5173")).rstrip("/")
SNOOZE_SECONDS = 5 * 60

# -----------------------------------------------------------------------------
# Desktop theme: inspired by the supplied mobile references, but redesigned
# for a wider laptop window. Blue/cyan replaces the original green accent.
# -----------------------------------------------------------------------------
BG = "#080C12"
SIDEBAR = "#0C121A"
CARD = "#101923"
CARD_ALT = "#151F2B"
CARD_HOVER = "#1A2736"
BORDER = "#1E3142"
TEXT = "#F4F7FB"
SECONDARY = "#9BAABB"
MUTED = "#65768A"
BLUE = "#3F86FF"
BLUE_BRIGHT = "#64A4FF"
BLUE_DARK = "#173B6F"
CYAN = "#45D9E8"
SUCCESS = "#45D69B"
WARNING = "#FFBF68"
DANGER = "#FF6377"
WHITE = "#FFFFFF"

FONT = "Segoe UI"

COMMON_APPS = [
    "Visual Studio Code", "Google Chrome", "Microsoft Edge", "Mozilla Firefox",
    "PyCharm", "IntelliJ IDEA", "Microsoft Word", "Microsoft Excel",
    "Microsoft PowerPoint", "Notepad", "PowerShell", "Windows Terminal",
    "File Explorer", "Jupyter Notebook",
]


class RoundedButton(tk.Frame):
    def __init__(self, master, text, command, width=160, primary=False, **kwargs):
        super().__init__(master, bg=kwargs.pop("bg", BG), width=width, height=40)
        self.command = command
        self.primary = primary
        self.label = tk.Label(
            self, text=text, bg=BLUE if primary else CARD_ALT, fg=WHITE,
            font=(FONT, 10, "bold" if primary else "normal"),
            padx=18, pady=10, cursor="hand2"
        )
        self.label.pack(fill="both", expand=True)
        self.label.bind("<Button-1>", lambda _e: self.command())
        self.label.bind("<Enter>", self._hover)
        self.label.bind("<Leave>", self._leave)

    def _hover(self, _event):
        self.label.configure(bg=BLUE_BRIGHT if self.primary else CARD_HOVER)

    def _leave(self, _event):
        self.label.configure(bg=BLUE if self.primary else CARD_ALT)


class TimerWindow(tk.Toplevel):
    """Small independent always-on-top timer."""
    def __init__(self, master):
        super().__init__(master)
        self.title("Focus Timer")
        self.geometry("270x185")
        self.minsize(250, 170)
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.hide)
        self.withdraw()

        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=18, pady=(14, 0))
        tk.Label(header, text="FOCUS TIMER", bg=BG, fg=SECONDARY,
                 font=(FONT, 9, "bold")).pack(side="left")
        tk.Button(header, text="×", command=self.hide, bg=BG, fg=MUTED,
                  activebackground=BG, activeforeground=TEXT, relief="flat", bd=0,
                  font=(FONT, 13), cursor="hand2").pack(side="right")

        self.title_label = tk.Label(self, text="Focus session", bg=BG, fg=TEXT,
                                    font=(FONT, 9, "bold"))
        self.title_label.pack(pady=(12, 0))
        self.timer_label = tk.Label(self, text="00:00", bg=BG, fg=TEXT,
                                    font=(FONT, 34, "bold"))
        self.timer_label.pack(pady=(2, 0))
        self.status_label = tk.Label(self, text="Waiting to start", bg=BG,
                                     fg=SECONDARY, font=(FONT, 9))
        self.status_label.pack()
        # The track is packed full width; the bar inside it is placed with a
        # relative width, so it actually shrinks as time runs out.
        track = tk.Frame(self, bg=CARD_ALT, height=4)
        track.pack(fill="x", padx=20, pady=(14, 0))
        self.progress = tk.Frame(track, bg=BLUE)
        self.progress.place(x=0, y=0, relheight=1, relwidth=1)

    def show(self, session_title="Focus session"):
        self.title_label.configure(text=session_title[:32])
        self.deiconify()
        self.attributes("-topmost", True)
        self.lift()
        self._position()

    def _position(self):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{sw - self.winfo_width() - 28}+36")

    def update_timer(self, remaining, total, active=True, status=None, color=None):
        remaining = max(0, int(remaining))
        minutes, seconds = divmod(remaining, 60)
        self.timer_label.configure(text=f"{minutes:02d}:{seconds:02d}")
        self.status_label.configure(
            text=status or ("Focus session active" if active else "Session ended"),
            fg=color or (SUCCESS if active else SECONDARY),
        )
        ratio = 0 if total <= 0 else max(0, min(1, remaining / total))
        self.progress.place_configure(relwidth=ratio)

    def hide(self):
        self.withdraw()


class DeviationPopup(tk.Toplevel):
    """Persistent non-modal overlay. It stays until the user returns to the
    zone, allows the application for this session, or snoozes it."""
    def __init__(self, master, on_allow=None, on_snooze=None):
        super().__init__(master)
        self.withdraw()
        self.title("Focus Deviation")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=DANGER)
        self.geometry("390x260")
        self.application = None
        self.on_allow = on_allow
        self.on_snooze = on_snooze

        outer = tk.Frame(self, bg=DANGER)
        outer.pack(fill="both", expand=True, padx=2, pady=2)
        inner = tk.Frame(outer, bg="#11151C")
        inner.pack(fill="both", expand=True, padx=2, pady=2)

        tk.Label(inner, text="●  FOCUS DEVIATION", bg="#11151C", fg=DANGER,
                 font=(FONT, 9, "bold")).pack(anchor="w", padx=20, pady=(16, 0))
        self.current = tk.Label(inner, text="", bg="#11151C", fg=TEXT,
                                font=(FONT, 15, "bold"), anchor="w")
        self.current.pack(fill="x", padx=20, pady=(12, 2))
        self.message = tk.Label(inner, text="", bg="#11151C", fg=SECONDARY,
                                font=(FONT, 9), justify="left", anchor="w",
                                wraplength=340)
        self.message.pack(fill="x", padx=20)
        self.goal = tk.Label(inner, text="", bg="#11151C", fg=MUTED,
                             font=(FONT, 8), justify="left", anchor="w",
                             wraplength=340)
        self.goal.pack(fill="x", padx=20, pady=(10, 0))
        tk.Label(inner, text="This reminder stays until you return to your focus zone.",
                 bg="#11151C", fg=BLUE_BRIGHT, font=(FONT, 8, "bold"),
                 wraplength=340, justify="left").pack(anchor="w", padx=20, pady=(10, 8))
        actions = tk.Frame(inner, bg="#11151C")
        actions.pack(anchor="w", padx=20, pady=(0, 14))
        RoundedButton(actions, "Allow for this session", self._allow,
                      width=170, primary=True, bg="#11151C").pack(side="left")
        RoundedButton(actions, "Snooze 5 min", self._snooze,
                      width=120, bg="#11151C").pack(side="left", padx=(8, 0))

    def _allow(self):
        if self.on_allow:
            self.on_allow(self.application)

    def _snooze(self):
        if self.on_snooze:
            self.on_snooze()

    def show_deviation(self, application, window_title, goal, allowed_apps):
        self.application = application
        self.current.configure(text=application or "Unknown application")
        detail = "Switch back to one of your selected applications."
        if window_title and window_title != "Unknown":
            detail = f"Current window: {window_title}\n\n{detail}"
        self.message.configure(text=detail)
        self.goal.configure(text=f"Goal: {goal}\nFocus zone: {', '.join(allowed_apps)}")
        self.deiconify()
        self.attributes("-topmost", True)
        self.lift()
        self._position()

    def _position(self):
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{sw - self.winfo_width() - 28}+{sh - self.winfo_height() - 72}")

    def hide_deviation(self):
        self.withdraw()


class FocusAssistant(tk.Tk):
    def __init__(self, data_dir=None):
        super().__init__()
        self.title("Focus Assistant")
        self.geometry("1180x780")
        self.minsize(1000, 700)
        self.configure(bg=BG)

        self.manager = FocusManager(data_dir)
        self.timer_window = TimerWindow(self)
        self.deviation_popup = DeviationPopup(
            self, on_allow=self._allow_application, on_snooze=self._snooze_deviation
        )

        self.current_tab = "focus"
        self.previous_app = None
        self.session_total_seconds = 0
        self.completion_prompt_open = False
        self.session_ended_message = ""
        self.snooze_until = 0.0
        self._last_monitor_error = None

        # Widgets of the focus tab are destroyed whenever another tab is
        # shown; their state lives here and is re-applied when it is rebuilt.
        self.live_dot = None
        self.live_dot_color = MUTED
        self.app_checkbuttons = []
        self.add_app_label = None
        self.dashboard_hint = None
        self.live_state = {"current_app": None, "allowed": None, "status": "No active session"}

        self.title_var = tk.StringVar()
        self.goal_var = tk.StringVar()
        self.duration_var = tk.StringVar(value="25")
        self.current_app_var = tk.StringVar(value="No active session")
        self.current_status_var = tk.StringVar(value="Ready when you are")
        self.selected_apps = {}
        self.custom_apps = []

        self._build_shell()
        self._show_tab("focus")
        self.after(1000, self._monitor_loop)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ---------------------------------------------------------------- shell
    def _build_shell(self):
        self.sidebar = tk.Frame(self, bg=SIDEBAR, width=245)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        brand = tk.Frame(self.sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=24, pady=(30, 28))
        tk.Label(brand, text="FOCUS", bg=SIDEBAR, fg=TEXT,
                 font=(FONT, 20, "bold")).pack(anchor="w")
        tk.Label(brand, text="ASSISTANT", bg=SIDEBAR, fg=BLUE_BRIGHT,
                 font=(FONT, 9, "bold")).pack(anchor="w")
        tk.Label(brand, text="Goal-aware focus workspace", bg=SIDEBAR, fg=MUTED,
                 font=(FONT, 8)).pack(anchor="w", pady=(5, 0))

        self.nav_buttons = {}
        nav = [
            ("focus", "Focus Session", "Start a focused work block"),
            ("history", "Past Sessions", "Review session-by-session detail"),
            ("analytics", "Focus Analytics", "Understand your focus patterns"),
            ("dashboard", "Workspace Dashboard", "Go deeper with workspace data"),
        ]
        for key, label, sub in nav:
            item = tk.Frame(self.sidebar, bg=SIDEBAR, cursor="hand2")
            item.pack(fill="x", padx=12, pady=4)
            dot = tk.Label(item, text="○", bg=SIDEBAR, fg=MUTED, font=(FONT, 15))
            dot.pack(side="left", padx=(10, 9))
            text = tk.Frame(item, bg=SIDEBAR)
            text.pack(side="left", fill="x", pady=8)
            title = tk.Label(text, text=label, bg=SIDEBAR, fg=SECONDARY,
                             font=(FONT, 10, "bold"), anchor="w")
            title.pack(anchor="w")
            tk.Label(text, text=sub, bg=SIDEBAR, fg=MUTED, font=(FONT, 7),
                     anchor="w").pack(anchor="w", pady=(2, 0))
            for widget in (item, dot, text, title):
                widget.bind("<Button-1>", lambda _e, k=key: self._show_tab(k))
            self.nav_buttons[key] = (item, dot, title)

        bottom = tk.Frame(self.sidebar, bg=SIDEBAR)
        bottom.pack(side="bottom", fill="x", padx=24, pady=24)
        tk.Label(bottom, text="CURRENT MONITOR", bg=SIDEBAR, fg=SECONDARY,
                 font=(FONT, 8, "bold")).pack(anchor="w")
        tk.Label(bottom, text="Watches the application in front while a session runs. Idle time is not counted.",
                 bg=SIDEBAR, fg=MUTED, font=(FONT, 7), wraplength=185,
                 justify="left").pack(anchor="w", pady=(5, 0))

        self.content = tk.Frame(self, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)

    def _clear_content(self):
        for child in self.content.winfo_children():
            child.destroy()

    def _set_active_nav(self, key):
        for name, (item, dot, title) in self.nav_buttons.items():
            active = name == key
            bg = "#122033" if active else SIDEBAR
            item.configure(bg=bg)
            dot.configure(bg=bg, fg=BLUE_BRIGHT if active else MUTED,
                          text="●" if active else "○")
            title.configure(bg=bg, fg=TEXT if active else SECONDARY)

    def _show_tab(self, key):
        self.current_tab = key
        self._set_active_nav(key)
        self._clear_content()
        if key == "focus":
            self._build_focus_tab()
        elif key == "history":
            self._build_history_tab()
        elif key == "analytics":
            self._build_analytics_tab()
        else:
            self._build_dashboard_tab()

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _safe_configure(widget, **kwargs):
        """Configure a widget only if it still exists (tabs destroy theirs)."""
        if widget is None:
            return False
        try:
            if not widget.winfo_exists():
                return False
            widget.configure(**kwargs)
        except tk.TclError:
            return False
        return True

    def _set_live_dot(self, color):
        self.live_dot_color = color
        self._safe_configure(self.live_dot, fg=color)

    def _header(self, eyebrow, title, subtitle):
        head = tk.Frame(self.content, bg=BG)
        head.pack(fill="x", padx=42, pady=(34, 22))
        tk.Label(head, text=eyebrow.upper(), bg=BG, fg=BLUE_BRIGHT,
                 font=(FONT, 8, "bold")).pack(anchor="w")
        tk.Label(head, text=title, bg=BG, fg=TEXT,
                 font=(FONT, 25, "bold")).pack(anchor="w", pady=(5, 3))
        tk.Label(head, text=subtitle, bg=BG, fg=SECONDARY,
                 font=(FONT, 9)).pack(anchor="w")

    def _card(self, parent, padx=20, pady=18):
        frame = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                         highlightthickness=1)
        frame.pack(fill="x", padx=padx, pady=pady)
        return frame

    def _field(self, parent, label, variable, placeholder):
        wrap = tk.Frame(parent, bg=CARD)
        wrap.pack(fill="x", padx=22, pady=(0, 12))
        tk.Label(wrap, text=label, bg=CARD, fg=SECONDARY,
                 font=(FONT, 8, "bold")).pack(anchor="w")
        entry = tk.Entry(wrap, textvariable=variable, bg=CARD_ALT, fg=TEXT,
                         insertbackground=TEXT, relief="flat", bd=0, font=(FONT, 10))
        entry.pack(fill="x", ipady=10, pady=(5, 0))
        tk.Label(wrap, text=placeholder, bg=CARD, fg=MUTED,
                 font=(FONT, 7)).pack(anchor="w", pady=(3, 0))

    # ------------------------------------------------------------- focus tab
    def _build_focus_tab(self):
        self._header("Focus workspace", "Start a focus session",
                     "Set an intention, choose your focus zone, and let the assistant watch for deviations.")

        body = tk.Frame(self.content, bg=BG)
        body.pack(fill="both", expand=True, padx=42)
        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, 10))
        right = tk.Frame(body, bg=BG, width=330)
        right.pack(side="right", fill="y", padx=(10, 0))
        right.pack_propagate(False)

        form = self._card(left, padx=0, pady=0)
        form.pack_configure(padx=0, pady=0)
        tk.Label(form, text="Session details", bg=CARD, fg=TEXT,
                 font=(FONT, 12, "bold")).pack(anchor="w", padx=22, pady=(20, 4))
        tk.Label(form, text="Name the work block and make the intention explicit.", bg=CARD,
                 fg=MUTED, font=(FONT, 8)).pack(anchor="w", padx=22, pady=(0, 16))
        self._field(form, "Session title", self.title_var, "e.g. Finish ML feature engineering")
        self._field(form, "Focus goal / reason", self.goal_var, "e.g. Complete preprocessing before the review")

        duration_row = tk.Frame(form, bg=CARD)
        duration_row.pack(fill="x", padx=22, pady=(4, 16))
        tk.Label(duration_row, text="Focus time", bg=CARD, fg=SECONDARY,
                 font=(FONT, 8, "bold")).pack(anchor="w")
        duration_controls = tk.Frame(duration_row, bg=CARD)
        duration_controls.pack(fill="x", pady=(6, 0))
        self.duration_spin = tk.Spinbox(
            duration_controls, from_=1, to=480, textvariable=self.duration_var,
            width=6, justify="center", bg=CARD_ALT, fg=TEXT,
            buttonbackground=CARD_ALT, insertbackground=TEXT,
            relief="flat", bd=0, font=(FONT, 13, "bold")
        )
        self.duration_spin.pack(side="left", ipady=7)
        tk.Label(duration_controls, text="minutes", bg=CARD, fg=SECONDARY,
                 font=(FONT, 9)).pack(side="left", padx=(8, 20))
        for minutes in (15, 25, 45, 60, 90):
            b = tk.Label(duration_controls, text=str(minutes), bg=CARD_ALT,
                         fg=SECONDARY, padx=10, pady=6, cursor="hand2",
                         font=(FONT, 8, "bold"))
            b.pack(side="left", padx=3)
            b.bind("<Button-1>", lambda _e, m=minutes: self.duration_var.set(str(m)))

        self._build_app_selector(form)
        buttons = tk.Frame(form, bg=CARD)
        buttons.pack(fill="x", padx=22, pady=(2, 22))
        self.start_button = RoundedButton(buttons, "Start Focus Session", self.start_focus,
                                          width=190, primary=True, bg=CARD)
        self.start_button.pack(side="left")
        self.stop_button = RoundedButton(buttons, "End Session", self.stop_focus,
                                         width=130, bg=CARD)
        self.stop_button.pack(side="left", padx=(10, 0))
        self._update_session_buttons()

        state = self._card(right, padx=0, pady=0)
        state.pack_configure(padx=0, pady=0)
        tk.Label(state, text="Live session", bg=CARD, fg=SECONDARY,
                 font=(FONT, 8, "bold")).pack(anchor="w", padx=20, pady=(18, 8))
        self.live_dot = tk.Label(state, text="●", bg=CARD, fg=self.live_dot_color, font=(FONT, 16))
        self.live_dot.pack(anchor="w", padx=20)
        tk.Label(state, textvariable=self.current_app_var, bg=CARD, fg=TEXT,
                 font=(FONT, 12, "bold"), wraplength=270, justify="left").pack(anchor="w", padx=20, pady=(5, 2))
        tk.Label(state, textvariable=self.current_status_var, bg=CARD, fg=SECONDARY,
                 font=(FONT, 8), wraplength=270, justify="left").pack(anchor="w", padx=20, pady=(0, 18))

        zone = self._card(right, padx=0, pady=0)
        zone.pack_configure(padx=0, pady=0)
        tk.Label(zone, text="Your focus zone", bg=CARD, fg=TEXT,
                 font=(FONT, 10, "bold")).pack(anchor="w", padx=20, pady=(18, 8))
        self.zone_list = tk.Frame(zone, bg=CARD)
        self.zone_list.pack(fill="x", padx=20, pady=(0, 16))
        self._refresh_zone_preview()

        note = self._card(right, padx=0, pady=0)
        note.pack_configure(padx=0, pady=0)
        tk.Label(note, text="What makes this different", bg=CARD, fg=TEXT,
                 font=(FONT, 10, "bold")).pack(anchor="w", padx=20, pady=(18, 7))
        tk.Label(note, text="The assistant does not label an app as productive or distracting by default. It compares what you are doing with what you said this session is for.",
                 bg=CARD, fg=SECONDARY, font=(FONT, 8), wraplength=275,
                 justify="left").pack(anchor="w", padx=20, pady=(0, 18))

    def _build_app_selector(self, parent):
        box = tk.Frame(parent, bg=CARD)
        box.pack(fill="x", padx=22, pady=(0, 16))
        tk.Label(box, text="Applications allowed in this session", bg=CARD,
                 fg=SECONDARY, font=(FONT, 8, "bold")).pack(anchor="w")
        tk.Label(box, text="Select each application individually. The zone is fixed while a session runs; "
                           "use 'Allow for this session' on the deviation reminder to add one.",
                 bg=CARD, fg=MUTED, font=(FONT, 7), wraplength=650,
                 justify="left").pack(anchor="w", pady=(3, 8))

        chips = tk.Frame(box, bg=CARD)
        chips.pack(fill="x")
        apps = list(dict.fromkeys(COMMON_APPS + self.custom_apps))
        running = self.manager.running
        self.app_checkbuttons = []
        for app in apps:
            if app not in self.selected_apps:
                self.selected_apps[app] = tk.BooleanVar(value=False)
            cb = tk.Checkbutton(
                chips, text=app, variable=self.selected_apps[app],
                command=self._refresh_zone_preview, bg=CARD_ALT, fg=SECONDARY,
                selectcolor=BLUE_DARK, activebackground=CARD_ALT,
                activeforeground=TEXT, relief="flat", bd=0, padx=8, pady=5,
                font=(FONT, 8), cursor="hand2",
                state="disabled" if running else "normal",
            )
            cb.pack(side="left", padx=(0, 6), pady=(0, 6))
            self.app_checkbuttons.append(cb)

        add = tk.Label(box, text="＋ Add another application", bg=CARD,
                       fg=MUTED if running else BLUE_BRIGHT, cursor="hand2",
                       font=(FONT, 8, "bold"))
        add.pack(anchor="w", pady=(2, 0))
        add.bind("<Button-1>", lambda _e: self._add_custom_app())
        self.add_app_label = add

    def _refresh_zone_preview(self):
        zone_list = getattr(self, "zone_list", None)
        try:
            if zone_list is None or not zone_list.winfo_exists():
                return
        except tk.TclError:
            return
        for child in zone_list.winfo_children():
            child.destroy()
        # While a session runs, show the zone it actually enforces.
        if self.manager.running:
            selected = list(self.manager.allowed_apps)
        else:
            selected = [app for app, var in self.selected_apps.items() if var.get()]
        if not selected:
            tk.Label(self.zone_list, text="No applications selected yet.", bg=CARD,
                     fg=MUTED, font=(FONT, 8)).pack(anchor="w")
            return
        for app in selected:
            row = tk.Frame(self.zone_list, bg=CARD)
            row.pack(fill="x", pady=2)
            tk.Label(row, text="●", bg=CARD, fg=BLUE_BRIGHT, font=(FONT, 9)).pack(side="left")
            tk.Label(row, text=app, bg=CARD, fg=SECONDARY,
                     font=(FONT, 8)).pack(side="left", padx=(7, 0))

    def _add_custom_app(self):
        if self.manager.running:
            return
        dialog = tk.Toplevel(self)
        dialog.title("Add application")
        dialog.geometry("390x175")
        dialog.configure(bg=CARD)
        dialog.attributes("-topmost", True)
        dialog.resizable(False, False)
        tk.Label(dialog, text="Add an application", bg=CARD, fg=TEXT,
                 font=(FONT, 13, "bold")).pack(anchor="w", padx=22, pady=(20, 4))
        tk.Label(dialog, text="Enter one application name.", bg=CARD, fg=SECONDARY,
                 font=(FONT, 8)).pack(anchor="w", padx=22)
        var = tk.StringVar()
        entry = tk.Entry(dialog, textvariable=var, bg=CARD_ALT, fg=TEXT,
                         insertbackground=TEXT, relief="flat", bd=0, font=(FONT, 10))
        entry.pack(fill="x", padx=22, pady=12, ipady=8)
        entry.focus_set()

        def add():
            name = var.get().strip()
            if not name:
                return
            if name not in self.custom_apps and name not in self.selected_apps:
                self.custom_apps.append(name)
            if name not in self.selected_apps:
                self.selected_apps[name] = tk.BooleanVar(value=True)
            else:
                self.selected_apps[name].set(True)
            dialog.destroy()
            self._show_tab("focus")

        RoundedButton(dialog, "Add application", add, width=150, primary=True, bg=CARD).pack(anchor="w", padx=22)

    # -------------------------------------------------------------- session
    def _selected_application_names(self):
        return [app for app, var in self.selected_apps.items() if var.get()]

    def start_focus(self):
        try:
            duration = int(self.duration_var.get())
        except ValueError:
            messagebox.showerror("Invalid time", "Enter a whole number of minutes.", parent=self)
            return

        try:
            self.manager.start_session(
                title=self.title_var.get(), goal=self.goal_var.get(),
                duration_minutes=duration, allowed_apps=self._selected_application_names()
            )
        except (ValueError, RuntimeError) as error:
            messagebox.showwarning("Focus session", str(error), parent=self)
            return

        self.session_total_seconds = duration * 60
        self.previous_app = None
        self.completion_prompt_open = False
        self.snooze_until = 0.0
        self.current_status_var.set("Session active — monitoring your declared focus zone")
        self._set_live_dot(SUCCESS)
        self.timer_window.show(self.manager.title)
        self.timer_window.update_timer(self.session_total_seconds, self.session_total_seconds, True)
        self.deviation_popup.hide_deviation()
        self._update_session_buttons()
        self._refresh_zone_preview()
        self._run_monitor_tick()

    def stop_focus(self, completed=False):
        if not self.manager.running:
            return
        self.manager.stop_session("Completed" if completed else "Stopped")
        self._finish_visual_state("Session completed" if completed else "Session ended")
        if self.current_tab in ("history", "analytics"):
            self._show_tab(self.current_tab)

    def _finish_visual_state(self, message):
        self.timer_window.hide()
        self.deviation_popup.hide_deviation()
        self.snooze_until = 0.0
        self.current_status_var.set(message)
        self._set_live_dot(SUCCESS if "completed" in message.lower() else MUTED)
        self._set_live_state(None, None, "No active session")
        self._update_session_buttons()
        self._refresh_zone_preview()

    def _update_session_buttons(self):
        running = self.manager.running
        start_button = getattr(self, "start_button", None)
        stop_button = getattr(self, "stop_button", None)
        if start_button is not None:
            self._safe_configure(start_button.label, bg=MUTED if running else BLUE,
                                 fg="#D9DEE7" if running else WHITE)
        if stop_button is not None:
            self._safe_configure(stop_button.label, bg=BLUE_DARK if running else CARD_ALT,
                                 fg=TEXT if running else MUTED)
        # The focus zone is fixed while a session runs.
        for checkbutton in self.app_checkbuttons:
            self._safe_configure(checkbutton, state="disabled" if running else "normal")
        self._safe_configure(self.add_app_label, fg=MUTED if running else BLUE_BRIGHT)

    def _handle_timer_completion(self):
        if self.completion_prompt_open or not self.manager.running:
            return
        self.completion_prompt_open = True
        due_at = self.manager.expected_end_at
        # Time spent answering the prompt is neither focus nor deviation.
        self.manager.mark_due()
        self.timer_window.hide()
        self.deviation_popup.hide_deviation()
        self.current_status_var.set("Planned focus time reached — choose what to do next")
        self._set_live_dot(BLUE_BRIGHT)
        self._set_live_state(None, None, "Planned focus time reached")

        continue_session = messagebox.askyesno(
            "Focus time completed",
            f"You completed your planned {self.manager.duration_minutes} minute focus block.\n\n"
            "Would you like to continue focusing?\n\n"
            "Yes = add more focus time\nNo = end the session and return to the main page",
            parent=self,
        )

        if continue_session:
            extra = simpledialog.askinteger(
                "Continue focus",
                "How many additional minutes would you like?",
                parent=self, minvalue=1, maxvalue=480, initialvalue=25
            )
            if extra:
                try:
                    remaining = self.manager.extend_session(extra)
                    self.session_total_seconds = max(extra * 60, remaining)
                    self.timer_window.show(self.manager.title)
                    self.timer_window.update_timer(remaining, self.session_total_seconds, True)
                    self.current_status_var.set(f"Focus extended by {extra} minutes")
                    self._set_live_dot(SUCCESS)
                    self.completion_prompt_open = False
                    self._run_monitor_tick()
                    return
                except (ValueError, RuntimeError) as error:
                    messagebox.showerror("Unable to continue", str(error), parent=self)

        self.completion_prompt_open = False
        # The session ended when the planned time ran out, not when the
        # prompt was answered.
        self.manager.stop_session("Completed", ended_at=due_at)
        self._finish_visual_state("Session completed")
        self._show_tab("focus")

    def _allow_application(self, application):
        """'Allow for this session' on the deviation reminder."""
        if not application or not self.manager.running:
            return
        self.manager.allow_application(application)
        self.deviation_popup.hide_deviation()
        self.current_status_var.set(f"{application} is now part of this session's focus zone")
        self._set_live_dot(SUCCESS)
        self._refresh_zone_preview()

    def _snooze_deviation(self):
        """'Snooze 5 min': hide the reminder; the deviation is still recorded."""
        self.snooze_until = time.monotonic() + SNOOZE_SECONDS
        self.deviation_popup.hide_deviation()
        self.current_status_var.set("Reminder snoozed for 5 minutes — deviation time is still recorded")

    # --------------------------------------------------------------- monitor
    def _monitor_loop(self):
        # Re-arm first, so an error in one tick can never stop monitoring and
        # ticks (and the heartbeat) keep running while a modal prompt is open.
        try:
            self.after(1000, self._monitor_loop)
        except tk.TclError:
            return
        self._run_monitor_tick()

    def _run_monitor_tick(self):
        try:
            self._monitor_once()
        except Exception as error:
            # Log each distinct error once instead of every second.
            message = f"{type(error).__name__}: {error}"
            if message != self._last_monitor_error:
                self._last_monitor_error = message
                LOGGER.exception("Focus monitor tick failed")
        finally:
            self._write_heartbeat()

    def _write_heartbeat(self):
        try:
            self.manager.write_heartbeat(
                current_app=self.live_state["current_app"],
                allowed=self.live_state["allowed"],
                status=self.live_state["status"],
            )
        except Exception:
            pass

    def _set_live_state(self, current_app, allowed, status):
        self.live_state = {"current_app": current_app, "allowed": allowed, "status": status}

    @staticmethod
    def _active_window():
        """(application, window_title, pid); pid is None when unavailable."""
        try:
            if get_active_process is not None:
                application, window_title, pid = get_active_process()
                return application, window_title, pid
            application, window_title = get_active_application()
            return application, window_title, None
        except Exception:
            return "Unknown", "Unknown", None

    @staticmethod
    def _idle_seconds():
        try:
            return float(get_idle_time() or 0.0)
        except Exception:
            return 0.0

    def _monitor_once(self):
        if not self.manager.running:
            if self.current_tab == "focus":
                self.current_app_var.set("No active session")
                self.current_status_var.set("Start a session to begin monitoring")
            self._set_live_state(None, None, "No active session")
            return

        if self.completion_prompt_open:
            return

        # After a sleep, log the time asleep as idle before anything else
        # (including the completion prompt for a planned end that passed
        # meanwhile) is recorded.
        self.manager.record_tick_gap()

        if self.manager.is_due():
            self._handle_timer_completion()
            return

        application, window_title, pid = self._active_window()
        result = self.manager.evaluate_application(
            application, window_title, pid=pid, idle_seconds=self._idle_seconds()
        )
        state = result["state"]
        remaining = self.manager.remaining_seconds()
        total = self.session_total_seconds

        if state == "self":
            # One of our own windows (main window, timer, the reminder that
            # is being clicked): it neither creates nor clears a deviation.
            self._set_live_state("Focus Assistant", None, "Focus Assistant")
            self.timer_window.update_timer(remaining, total, True)
            return

        if state == "unknown":
            # The active window could not be identified: no event, no popup.
            self._set_live_state(None, None, "Active window unknown")
            self.timer_window.update_timer(remaining, total, True)
            return

        self.current_app_var.set(application)

        if state == "idle":
            self.current_status_var.set("Paused (idle) — time away is not counted")
            self._set_live_dot(WARNING)
            self.deviation_popup.hide_deviation()
            self._set_live_state(application, None, "Paused (idle)")
            self.timer_window.update_timer(remaining, total, True,
                                           status="Paused (idle)", color=WARNING)
        elif state == "neutral":
            # System windows (task switcher, desktop, Spotlight): not counted
            # and they leave the reminder as it is.
            self.current_status_var.set("System window — not counted")
            self._set_live_state(application, None, "Neutral")
            self.timer_window.update_timer(remaining, total, True)
        elif result["allowed"]:
            self.current_status_var.set("Within your declared focus zone")
            self._set_live_dot(SUCCESS)
            self.deviation_popup.hide_deviation()
            self._set_live_state(application, True, result["status"])
            self.timer_window.update_timer(remaining, total, True)
        else:
            self._set_live_dot(DANGER)
            self._set_live_state(application, False, result["status"])
            if time.monotonic() < self.snooze_until:
                self.current_status_var.set("Deviation detected — reminder snoozed")
            else:
                self.current_status_var.set("Deviation detected — return to your focus zone")
                self._show_persistent_deviation(application, window_title)
            self.timer_window.update_timer(remaining, total, True)

        if self.manager.last_write_error:
            self.current_status_var.set(self.manager.last_write_error)
        self.previous_app = application

    def _show_persistent_deviation(self, application, window_title):
        self.deviation_popup.show_deviation(
            application, window_title, self.manager.goal, self.manager.allowed_apps
        )

    # --------------------------------------------------------------- data
    def _session_records(self):
        """All sessions, newest first (accounting lives in focus/analytics.py)."""
        active = self.manager.session_id if self.manager.running else None
        return analytics.session_records(self.manager.data_dir, active_session_id=active)

    @staticmethod
    def _fmt_seconds(seconds):
        return analytics.fmt_seconds(seconds)

    @staticmethod
    def _fmt_pct(value):
        return "—" if value is None else f"{value:.0f}%"

    # --------------------------------------------------------------- history
    def _build_history_tab(self):
        self._header("Focus history", "Past focus sessions",
                     "Expand any session to see intent, focus time, deviations, app switching and focus-zone adherence.")
        records = self._session_records()
        if not records:
            empty = self._card(self.content, padx=42, pady=0)
            tk.Label(empty, text="No focus sessions yet", bg=CARD, fg=TEXT,
                     font=(FONT, 15, "bold")).pack(anchor="w", padx=22, pady=(22, 5))
            tk.Label(empty, text="Start your first session and its detailed record will appear here.",
                     bg=CARD, fg=SECONDARY, font=(FONT, 9)).pack(anchor="w", padx=22, pady=(0, 22))
            return

        holder = tk.Frame(self.content, bg=BG)
        holder.pack(fill="both", expand=True, padx=42, pady=(0, 25))
        canvas = tk.Canvas(holder, bg=BG, highlightthickness=0)
        scroll = tk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg=BG)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
        for record in records:
            self._history_card(inner, record)

    def _history_card(self, parent, record):
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1, cursor="hand2")
        card.pack(fill="x", pady=6)
        top = tk.Frame(card, bg=CARD)
        top.pack(fill="x", padx=18, pady=(14, 6))
        tk.Label(top, text="○", bg=CARD, fg=BLUE_BRIGHT, font=(FONT, 16)).pack(side="left")
        info = tk.Frame(top, bg=CARD)
        info.pack(side="left", fill="x", expand=True, padx=(8, 0))
        tk.Label(info, text=record["title"], bg=CARD, fg=TEXT,
                 font=(FONT, 11, "bold"), anchor="w").pack(anchor="w")
        date_text = record["start"].strftime("%d %b %Y  •  %I:%M %p") if record["start"] else "Unknown time"
        tk.Label(info, text=date_text, bg=CARD, fg=MUTED, font=(FONT, 8)).pack(anchor="w", pady=(3, 0))
        status_color = {
            analytics.STATUS_COMPLETED: SUCCESS,
            analytics.STATUS_IN_PROGRESS: WARNING,
            analytics.STATUS_INTERRUPTED: DANGER,
        }.get(record["status"], SECONDARY)
        tk.Label(top, text=record["status"], bg=CARD_ALT, fg=status_color,
                 font=(FONT, 8, "bold"), padx=10, pady=5).pack(side="right")
        arrow = tk.Label(top, text="›", bg=CARD, fg=MUTED, font=(FONT, 16))
        arrow.pack(side="right", padx=(12, 0))

        summary = tk.Frame(card, bg=CARD)
        summary.pack(fill="x", padx=18, pady=(0, 14))
        self._metric_inline(summary, "Planned", f"{record['planned']} min")
        self._metric_inline(summary, "Actual", self._fmt_seconds(record["actual_seconds"]))
        self._metric_inline(summary, "Zone adherence", self._fmt_pct(record["adherence"]))
        self._metric_inline(summary, "Deviations", str(record["deviations"]))
        self._metric_inline(summary, "App switches", str(record["switches"]))

        detail = tk.Frame(card, bg=CARD_ALT)
        detail.pack(fill="x", padx=12, pady=(0, 12))
        detail.pack_forget()
        details = [
            ("Goal / reason", record["goal"] or "Not recorded"),
            ("Declared focus zone", "  •  ".join(record["apps"]) or "None recorded"),
            ("Time outside zone", self._fmt_seconds(record["deviation_seconds"])),
            ("Longest uninterrupted focus period", self._fmt_seconds(record["longest_focus_seconds"])),
            ("Most time outside zone", record["top_deviation_app"] or "None"),
            ("Interpretation", self._session_interpretation(record)),
        ]
        for label, value in details:
            tk.Label(detail, text=label.upper(), bg=CARD_ALT, fg=MUTED,
                     font=(FONT, 7, "bold")).pack(anchor="w", padx=14, pady=(10, 2))
            tk.Label(detail, text=value, bg=CARD_ALT, fg=SECONDARY,
                     font=(FONT, 9), wraplength=820, justify="left").pack(anchor="w", padx=14)
        tk.Label(detail, text="These are behavioral focus indicators, not a universal judgement of whether an app is productive.",
                 bg=CARD_ALT, fg=MUTED, font=(FONT, 7), wraplength=820,
                 justify="left").pack(anchor="w", padx=14, pady=(10, 12))

        expanded = [False]
        def toggle(_event=None):
            expanded[0] = not expanded[0]
            if expanded[0]:
                detail.pack(fill="x", padx=12, pady=(0, 12)); arrow.configure(text="⌄")
            else:
                detail.pack_forget(); arrow.configure(text="›")
        for widget in (card, top, arrow):
            widget.bind("<Button-1>", toggle)

    def _metric_inline(self, parent, label, value):
        block = tk.Frame(parent, bg=CARD)
        block.pack(side="left", fill="x", expand=True, padx=(0, 8))
        tk.Label(block, text=label, bg=CARD, fg=MUTED, font=(FONT, 7)).pack(anchor="w")
        tk.Label(block, text=value, bg=CARD, fg=TEXT, font=(FONT, 9, "bold")).pack(anchor="w", pady=(2, 0))

    def _session_interpretation(self, r):
        if not r["actual_seconds"] or r["adherence"] is None:
            return "There is not enough timing data to interpret this session yet."
        if r["adherence"] >= 90:
            return "Most of the recorded session stayed inside the focus zone you declared."
        if r["adherence"] >= 70:
            return "The session contained a meaningful amount of in-zone time, with some interruptions outside the declared zone."
        return "A substantial portion of the recorded session occurred outside the declared focus zone; the deviation history can show where that time went."

    # -------------------------------------------------------------- analytics
    def _build_analytics_tab(self):
        self._header(
            "Focus analytics",
            "Understand your focus patterns",
            "Focus Assistant measures session behavior: time, consistency, interruptions, recovery and adherence to your own focus intent."
        )

        records = self._session_records()

        if not records:
            empty = self._card(self.content, padx=42, pady=0)

            tk.Label(
                empty,
                text="Your focus profile will appear here after your first session.",
                bg=CARD,
                fg=TEXT,
                font=(FONT, 13, "bold")
            ).pack(anchor="w", padx=22, pady=(22, 5))

            tk.Label(
                empty,
                text="Charts and behavioral indicators are calculated only from Focus Assistant data.",
                bg=CARD,
                fg=SECONDARY,
                font=(FONT, 9)
            ).pack(anchor="w", padx=22, pady=(0, 22))

            return

        # ---------------------------------------------------------
        # Calculate analytics
        # ---------------------------------------------------------

        summary = analytics.summarize(records)
        total = len(records)
        finished = sum(not r["running"] for r in records)
        completed = summary["completed"]
        # Focus time is in-zone time; session time is wall-clock time in
        # sessions (it includes idle, system-window and deviation time).
        in_zone = summary["focus_seconds_total"]
        actual = summary["session_seconds_total"]
        planned = sum(r["planned"] * 60 for r in records)
        deviations = summary["deviation_count"]
        outside = sum(r["deviation_seconds"] for r in records)
        # Time-weighted over finished sessions; idle and system-window time
        # count as neither focus nor deviation.
        adherence = summary["adherence_pct"]
        avg_session = actual / total if total else 0
        avg_deviation = outside / deviations if deviations else 0
        longest = max(
            (r["longest_focus_seconds"] for r in records),
            default=0
        )
        switches = sum(r["switches"] for r in records)

        metrics = [
            (
                "Focus time",
                self._fmt_seconds(in_zone),
                f"in your focus zone, of {self._fmt_seconds(actual)} in sessions "
                f"({self._fmt_seconds(planned)} planned)"
            ),
            (
                "Zone adherence",
                self._fmt_pct(adherence),
                "share of active session time inside your declared zone"
            ),
            (
                "Completion rate",
                self._fmt_pct(completed / finished * 100 if finished else None),
                f"{completed} of {finished} finished sessions completed"
            ),
            (
                "Avg. session",
                self._fmt_seconds(avg_session),
                "average recorded session length"
            ),
            (
                "Longest focus run",
                self._fmt_seconds(longest),
                "longest recorded uninterrupted in-zone period"
            ),
            (
                "App switches",
                str(switches),
                "switches between applications during sessions"
            ),
        ]

        # =========================================================
        # SCROLLABLE ANALYTICS AREA
        # =========================================================

        holder = tk.Frame(self.content, bg=BG)
        holder.pack(
            fill="both",
            expand=True,
            padx=0,
            pady=0
        )

        canvas = tk.Canvas(
            holder,
            bg=BG,
            highlightthickness=0,
            bd=0
        )

        scrollbar = tk.Scrollbar(
            holder,
            orient="vertical",
            command=canvas.yview
        )

        canvas.configure(
            yscrollcommand=scrollbar.set
        )

        scrollbar.pack(
            side="right",
            fill="y"
        )

        canvas.pack(
            side="left",
            fill="both",
            expand=True
        )

        # This frame contains ALL analytics content.
        inner = tk.Frame(canvas, bg=BG)

        window_id = canvas.create_window(
            (0, 0),
            window=inner,
            anchor="nw"
        )

        # Update scrollable region whenever content changes.
        inner.bind(
            "<Configure>",
            lambda _event: canvas.configure(
                scrollregion=canvas.bbox("all")
            )
        )

        # Keep inner frame the same width as the canvas.
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(
                window_id,
                width=event.width
            )
        )

        # ---------------------------------------------------------
        # Mouse wheel scrolling
        # ---------------------------------------------------------

        def _on_mousewheel(event):
            canvas.yview_scroll(
                int(-1 * (event.delta / 120)),
                "units"
            )

        canvas.bind_all(
            "<MouseWheel>",
            _on_mousewheel
        )

        # =========================================================
        # METRIC CARDS
        # =========================================================

        grid = tk.Frame(inner, bg=BG)

        grid.pack(
            fill="x",
            padx=42,
            pady=(0, 5)
        )

        for i, metric in enumerate(metrics):
            row, col = divmod(i, 3)

            self._analytics_card_at(
                grid,
                metric,
                row,
                col
            )

        # =========================================================
        # CHARTS
        # =========================================================

        charts = tk.Frame(inner, bg=BG)

        charts.pack(
            fill="x",
            padx=42,
            pady=(18, 28)
        )

        left = tk.Frame(
            charts,
            bg=CARD,
            highlightbackground=BORDER,
            highlightthickness=1
        )

        left.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(0, 8)
        )

        right = tk.Frame(
            charts,
            bg=CARD,
            highlightbackground=BORDER,
            highlightthickness=1
        )

        right.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(8, 0)
        )

        tk.Label(
            left,
            text="Focus-zone adherence by session",
            bg=CARD,
            fg=TEXT,
            font=(FONT, 11, "bold")
        ).pack(
            anchor="w",
            padx=20,
            pady=(18, 2)
        )

        tk.Label(
            left,
            text="Higher means more of that session stayed within the apps you selected.",
            bg=CARD,
            fg=MUTED,
            font=(FONT, 8)
        ).pack(
            anchor="w",
            padx=20
        )

        # Records are newest first: chart the latest eight sessions.
        self._bar_chart(
            left,
            analytics.recent_records(records, 8),
            "adherence",
            100,
            "%"
        )

        tk.Label(
            right,
            text="Where deviations are coming from",
            bg=CARD,
            fg=TEXT,
            font=(FONT, 11, "bold")
        ).pack(
            anchor="w",
            padx=20,
            pady=(18, 2)
        )

        tk.Label(
            right,
            text=(
                f"{self._fmt_seconds(outside)} outside the declared zone "
                f"• average deviation {self._fmt_seconds(avg_deviation)}"
            ),
            bg=CARD,
            fg=MUTED,
            font=(FONT, 8)
        ).pack(
            anchor="w",
            padx=20
        )

        self._deviation_chart(
            right,
            summary["deviation_sources"]
        )

        # =========================================================
        # INTERPRETATION / INSIGHTS
        # =========================================================

        insight = self._card(
            inner,
            padx=42,
            pady=0
        )

        tk.Label(
            insight,
            text="What your current data suggests",
            bg=CARD,
            fg=TEXT,
            font=(FONT, 11, "bold")
        ).pack(
            anchor="w",
            padx=20,
            pady=(18, 5)
        )

        tk.Label(
            insight,
            text=self._overall_interpretation(
                records,
                adherence,
                outside,
                deviations
            ),
            bg=CARD,
            fg=SECONDARY,
            font=(FONT, 9),
            wraplength=900,
            justify="left"
        ).pack(
            anchor="w",
            padx=20,
            pady=(0, 18)
        )

        # Extra bottom spacing so the final card isn't pressed
        # against the bottom edge while scrolling.
        tk.Frame(
            inner,
            bg=BG,
            height=30
        ).pack(
            fill="x"
        )
    def _analytics_card_at(self, parent, metric, row, col):
        label, value, sub = metric
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
        parent.grid_columnconfigure(col, weight=1)
        tk.Label(card, text=label.upper(), bg=CARD, fg=MUTED,
                 font=(FONT, 7, "bold")).pack(anchor="w", padx=16, pady=(13, 3))
        tk.Label(card, text=value, bg=CARD, fg=TEXT,
                 font=(FONT, 18, "bold")).pack(anchor="w", padx=16)
        tk.Label(card, text=sub, bg=CARD, fg=SECONDARY, font=(FONT, 7),
                 wraplength=230, justify="left").pack(anchor="w", padx=16, pady=(3, 13))

    def _bar_chart(self, parent, records, field, maximum, suffix):
        canvas = tk.Canvas(parent, bg=CARD, highlightthickness=0, height=245)
        canvas.pack(fill="both", expand=True, padx=16, pady=12)
        def draw(_event=None):
            canvas.delete("all")
            w = max(400, canvas.winfo_width())
            h = max(220, canvas.winfo_height())
            left, right, top = 145, 22, 18
            row_h = max(25, (h - 30) / max(1, len(records)))
            for i, r in enumerate(records):
                y = top + i * row_h
                raw = r.get(field)
                label = r["title"][:20]
                canvas.create_text(0 + 2, y + 10, text=label, fill=SECONDARY,
                                   font=(FONT, 8), anchor="w")
                if raw is None:
                    # Nothing measurable yet (e.g. only idle or system windows).
                    canvas.create_text(left, y + 10, text="no data", fill=MUTED,
                                       font=(FONT, 8), anchor="w")
                    continue
                value = float(raw)
                bar_w = max(2, (w - left - right) * min(1, value / maximum))
                canvas.create_rectangle(left, y + 3, left + bar_w, y + 17,
                                        fill=BLUE, outline="")
                canvas.create_text(left + bar_w + 7, y + 10, text=f"{value:.0f}{suffix}",
                                   fill=TEXT, font=(FONT, 8, "bold"), anchor="w")
        canvas.bind("<Configure>", draw)

    def _deviation_chart(self, parent, sources):
        # Per-application time outside the zone, summed over all sessions.
        ranked = [(s["app"], s["seconds"]) for s in sources][:7]
        canvas = tk.Canvas(parent, bg=CARD, highlightthickness=0, height=245)
        canvas.pack(fill="both", expand=True, padx=16, pady=12)
        def draw(_event=None):
            canvas.delete("all")
            w, h = max(400, canvas.winfo_width()), max(220, canvas.winfo_height())
            if not ranked:
                canvas.create_text(w / 2, h / 2, text="No deviations recorded yet.",
                                   fill=MUTED, font=(FONT, 9))
                return
            max_v = max(v for _, v in ranked) or 1
            left, right, top = 145, 28, 18
            row_h = max(25, (h - 30) / len(ranked))
            for i, (app, seconds) in enumerate(ranked):
                y = top + i * row_h
                canvas.create_text(2, y + 10, text=app[:20], fill=SECONDARY,
                                   font=(FONT, 8), anchor="w")
                bar_w = max(2, (w - left - right) * seconds / max_v)
                canvas.create_rectangle(left, y + 3, left + bar_w, y + 17,
                                        fill=DANGER, outline="")
                canvas.create_text(left + bar_w + 7, y + 10, text=self._fmt_seconds(seconds),
                                   fill=TEXT, font=(FONT, 8, "bold"), anchor="w")
        canvas.bind("<Configure>", draw)

    def _overall_interpretation(self, records, adherence, outside, deviations):
        if adherence is None:
            first = "There is not enough finished, active session time yet to judge focus-zone adherence."
        elif adherence >= 90:
            first = "Your recorded sessions are generally staying close to the focus zones you define."
        elif adherence >= 70:
            first = "Your sessions show a mix of sustained focus and interruptions outside the declared zone."
        else:
            first = "Your current sessions contain substantial time outside the declared focus zones."
        if deviations == 0:
            second = "No deviation events have been recorded so far, so there is not yet a pattern of interruption to analyze."
        else:
            second = f"Across the recorded sessions, {deviations} deviation periods account for about {self._fmt_seconds(outside)} outside-zone time."
        return first + " " + second + " These indicators describe alignment with your stated focus intention; they should not be read as a universal measure of personal productivity."

    # ------------------------------------------------------------- dashboard
    def _build_dashboard_tab(self):
        self._header("Workspace analytics", "Today at a glance",
                     "Your focus sessions so far today. The web dashboard shows them next to your workspace activity.")
        records = self._session_records()
        today = analytics.today_summary(records)
        card = self._card(self.content, padx=42, pady=0)
        tk.Label(card, text="Today", bg=CARD, fg=TEXT,
                 font=(FONT, 16, "bold")).pack(anchor="w", padx=24, pady=(24, 7))
        if today["sessions"]:
            intro = ("Sessions that ran today. Focus time is time in your focus zone since midnight; "
                     "adherence counts finished sessions only.")
        else:
            intro = "No focus session today yet. Start one from the Focus Session tab."
        tk.Label(card, text=intro, bg=CARD, fg=SECONDARY, font=(FONT, 9), wraplength=820,
                 justify="left").pack(anchor="w", padx=24)
        tiles = tk.Frame(card, bg=CARD)
        tiles.pack(fill="x", padx=24, pady=20)
        for title, value in [
            ("Focus time", self._fmt_seconds(today["focus_seconds"])),
            ("Sessions", str(today["sessions"])),
            ("Zone adherence", self._fmt_pct(today["adherence_pct"])),
            ("Deviations", str(today["deviations"])),
        ]:
            f = tk.Frame(tiles, bg=CARD_ALT)
            f.pack(side="left", fill="both", expand=True, padx=(0, 8))
            tk.Label(f, text=title.upper(), bg=CARD_ALT, fg=MUTED,
                     font=(FONT, 7, "bold")).pack(anchor="w", padx=14, pady=(14, 5))
            tk.Label(f, text=value, bg=CARD_ALT, fg=TEXT,
                     font=(FONT, 16, "bold")).pack(anchor="w", padx=14, pady=(0, 14))
        RoundedButton(card, "Open the Focus page in the dashboard", self._open_dashboard,
                      width=260, primary=True, bg=CARD).pack(anchor="w", padx=24)
        self.dashboard_hint = tk.Label(card, text=f"Opens {DASHBOARD_URL}/focus", bg=CARD,
                                       fg=MUTED, font=(FONT, 8), wraplength=820, justify="left")
        self.dashboard_hint.pack(anchor="w", padx=24, pady=(8, 24))

    def _open_dashboard(self):
        url = f"{DASHBOARD_URL}/focus"
        if dashboard_reachable(DASHBOARD_URL):
            webbrowser.open(url)
            self._safe_configure(self.dashboard_hint, text=f"Opened {url}", fg=MUTED)
        else:
            self._safe_configure(
                self.dashboard_hint,
                text=f"The dashboard is not running at {DASHBOARD_URL}. Start it with python start_project.py",
                fg=WARNING,
            )

    def _close(self):
        if self.manager.running:
            answer = messagebox.askyesno(
                "End focus session?",
                "A focus session is still running. End it before closing Focus Assistant?",
                parent=self,
            )
            if not answer:
                return
            self.stop_focus(False)
        # Tell the dashboard right away that no session is running.
        self._set_live_state(None, None, "Focus Assistant closed")
        self._write_heartbeat()
        self.timer_window.destroy()
        self.deviation_popup.destroy()
        self.destroy()


def dashboard_reachable(url, timeout=1.0):
    """True if something answers at `url` within `timeout` seconds."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status < 500
    except urllib.error.HTTPError as error:
        return error.code < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def already_running_message(pid=None):
    who = f" (PID {pid})" if pid else ""
    return (
        f"Focus Assistant is already running{who}. Use its window, or close it "
        "before starting another one: two assistants would record over each other."
    )


def _report_already_running(message):
    print(message, file=sys.stderr)
    # Started from a launcher or a shortcut there may be no terminal to
    # read, so say it in a dialog too.
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("Focus Assistant", message, parent=root)
        root.destroy()
    except Exception:
        pass


def launch_focus_assistant(data_dir=None):
    """Run the assistant; returns the process exit code.

    Only one assistant runs per data directory: a second launch says so
    and returns 1 without opening a window.
    """
    lock = InstanceLock(data_dir)
    if not lock.acquire():
        _report_already_running(already_running_message(lock.holder_pid()))
        return 1
    if lock.error:
        LOGGER.warning("%s; another Focus Assistant would not be detected.", lock.error)
    try:
        app = FocusAssistant(data_dir)
        app.mainloop()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(launch_focus_assistant())
