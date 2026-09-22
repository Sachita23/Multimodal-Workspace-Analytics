import csv
import sys
import time
import webbrowser
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, simpledialog

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from collector.collector import get_active_application
from focus.focus_manager import FocusManager, FOCUS_SESSIONS_FILE, FOCUS_EVENTS_FILE

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
        self.progress = tk.Frame(self, bg=BLUE, height=4)
        self.progress.pack(fill="x", padx=20, pady=(14, 0))

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

    def update_timer(self, remaining, total, active=True):
        remaining = max(0, int(remaining))
        minutes, seconds = divmod(remaining, 60)
        self.timer_label.configure(text=f"{minutes:02d}:{seconds:02d}")
        self.status_label.configure(
            text="Focus session active" if active else "Session ended",
            fg=SUCCESS if active else SECONDARY,
        )
        ratio = 0 if total <= 0 else max(0, min(1, remaining / total))
        self.progress.configure(width=max(1, int(225 * ratio)))

    def hide(self):
        self.withdraw()


class DeviationPopup(tk.Toplevel):
    """Persistent non-modal overlay. It stays until the user returns to zone."""
    def __init__(self, master):
        super().__init__(master)
        self.withdraw()
        self.title("Focus Deviation")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=DANGER)
        self.geometry("390x205")

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
        tk.Label(inner, text="This reminder will remain until you return to your focus zone.",
                 bg="#11151C", fg=BLUE_BRIGHT, font=(FONT, 8, "bold"),
                 wraplength=340, justify="left").pack(anchor="w", padx=20, pady=(10, 14))

    def show_deviation(self, application, window_title, goal, allowed_apps):
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
    def __init__(self):
        super().__init__()
        self.title("Focus Assistant")
        self.geometry("1180x780")
        self.minsize(1000, 700)
        self.configure(bg=BG)

        self.manager = FocusManager()
        self.timer_window = TimerWindow(self)
        self.deviation_popup = DeviationPopup(self)

        self.current_tab = "focus"
        self.previous_app = None
        self.session_total_seconds = 0
        self.completion_prompt_open = False
        self.session_ended_message = ""

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
        tk.Label(bottom, text="Application-level first. Browser context comes next.",
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
        self.live_dot = tk.Label(state, text="●", bg=CARD, fg=MUTED, font=(FONT, 16))
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
        tk.Label(box, text="Select each application individually. Browser/tab intelligence will be added after app-level monitoring is stable.",
                 bg=CARD, fg=MUTED, font=(FONT, 7), wraplength=650,
                 justify="left").pack(anchor="w", pady=(3, 8))

        chips = tk.Frame(box, bg=CARD)
        chips.pack(fill="x")
        apps = list(dict.fromkeys(COMMON_APPS + self.custom_apps))
        for app in apps:
            if app not in self.selected_apps:
                self.selected_apps[app] = tk.BooleanVar(value=False)
            cb = tk.Checkbutton(
                chips, text=app, variable=self.selected_apps[app],
                command=self._refresh_zone_preview, bg=CARD_ALT, fg=SECONDARY,
                selectcolor=BLUE_DARK, activebackground=CARD_ALT,
                activeforeground=TEXT, relief="flat", bd=0, padx=8, pady=5,
                font=(FONT, 8), cursor="hand2"
            )
            cb.pack(side="left", padx=(0, 6), pady=(0, 6))

        add = tk.Label(box, text="＋ Add another application", bg=CARD,
                       fg=BLUE_BRIGHT, cursor="hand2", font=(FONT, 8, "bold"))
        add.pack(anchor="w", pady=(2, 0))
        add.bind("<Button-1>", lambda _e: self._add_custom_app())

    def _refresh_zone_preview(self):
        if not hasattr(self, "zone_list"):
            return
        for child in self.zone_list.winfo_children():
            child.destroy()
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
        self.current_status_var.set("Session active — monitoring your declared focus zone")
        self.live_dot.configure(fg=SUCCESS)
        self.timer_window.show(self.manager.title)
        self.timer_window.update_timer(self.session_total_seconds, self.session_total_seconds, True)
        self.deviation_popup.hide_deviation()
        self._update_session_buttons()
        self._monitor_once()

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
        self.current_status_var.set(message)
        self.live_dot.configure(fg=SUCCESS if "completed" in message.lower() else MUTED)
        self._update_session_buttons()

    def _update_session_buttons(self):
        running = self.manager.running
        if hasattr(self, "start_button"):
            self.start_button.label.configure(bg=MUTED if running else BLUE,
                                              fg="#D9DEE7" if running else WHITE)
        if hasattr(self, "stop_button"):
            self.stop_button.label.configure(bg=BLUE_DARK if running else CARD_ALT,
                                             fg=TEXT if running else MUTED)

    def _handle_timer_completion(self):
        if self.completion_prompt_open or not self.manager.running:
            return
        self.completion_prompt_open = True
        self.timer_window.hide()
        self.deviation_popup.hide_deviation()
        self.current_status_var.set("Planned focus time reached — choose what to do next")
        self.live_dot.configure(fg=BLUE_BRIGHT)

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
                    self.manager.extend_session(extra)
                    self.session_total_seconds = extra * 60
                    self.timer_window.show(self.manager.title)
                    self.timer_window.update_timer(self.session_total_seconds,
                                                  self.session_total_seconds, True)
                    self.current_status_var.set(f"Focus extended by {extra} minutes")
                    self.live_dot.configure(fg=SUCCESS)
                    self.completion_prompt_open = False
                    self._monitor_once()
                    return
                except (ValueError, RuntimeError) as error:
                    messagebox.showerror("Unable to continue", str(error), parent=self)

        self.completion_prompt_open = False
        self.manager.stop_session("Completed")
        self._finish_visual_state("Session completed")
        self._show_tab("focus")

    # --------------------------------------------------------------- monitor
    def _monitor_loop(self):
        self._monitor_once()
        self.after(1000, self._monitor_loop)

    def _monitor_once(self):
        try:
            application, window_title = get_active_application()
        except Exception:
            application, window_title = "Unknown", "Unknown"

        # Our own timer/deviation windows can become the foreground window on
        # some Windows configurations. Never let those helper windows create
        # or clear a user deviation. The next poll will inspect the real app
        # again after the user changes focus.
        internal_title = (window_title or "").strip().lower()
        if internal_title in {"focus deviation", "focus timer"} and self.manager.running:
            remaining = self.manager.remaining_seconds()
            if not self.completion_prompt_open:
                self.timer_window.update_timer(remaining, self.session_total_seconds, True)
            return

        if not self.manager.running:
            if self.current_tab == "focus":
                self.current_app_var.set("No active session")
                self.current_status_var.set("Start a session to begin monitoring")
            return

        if self.completion_prompt_open:
            return

        if self.manager.is_due():
            self._handle_timer_completion()
            return

        result = self.manager.evaluate_application(application, window_title)
        self.current_app_var.set(application)

        if result["allowed"]:
            self.current_status_var.set("Within your declared focus zone")
            self.live_dot.configure(fg=SUCCESS)
            self.deviation_popup.hide_deviation()
        else:
            self.current_status_var.set("Deviation detected — return to your focus zone")
            self.live_dot.configure(fg=DANGER)
            self._show_persistent_deviation(application, window_title)

        remaining = self.manager.remaining_seconds()
        self.timer_window.update_timer(remaining, self.session_total_seconds, True)
        self.previous_app = application

    def _show_persistent_deviation(self, application, window_title):
        self.deviation_popup.show_deviation(
            application, window_title, self.manager.goal, self.manager.allowed_apps
        )

    # --------------------------------------------------------------- data
    def _read_csv(self, path):
        if not path.exists():
            return []
        try:
            with open(path, "r", newline="", encoding="utf-8") as file:
                return list(csv.DictReader(file))
        except (OSError, csv.Error):
            return []

    def _session_records(self):
        rows = self._read_csv(FOCUS_SESSIONS_FILE)
        starts, ends, latest = {}, {}, {}
        for row in rows:
            sid = row.get("session_id", "")
            if not sid:
                continue
            latest[sid] = row
            if row.get("event_type") == "START":
                starts[sid] = row
            elif row.get("event_type") == "END":
                ends[sid] = row

        events = self._read_csv(FOCUS_EVENTS_FILE)
        grouped = {}
        for event in events:
            sid = event.get("focus_session_id", "")
            if sid:
                grouped.setdefault(sid, []).append(event)
        for sid in grouped:
            grouped[sid].sort(key=lambda r: self._parse_dt(r.get("timestamp")) or datetime.min)

        records = []
        for sid, start in starts.items():
            start_dt = self._parse_dt(start.get("timestamp"))
            end_row = ends.get(sid)
            end_dt = self._parse_dt(end_row.get("timestamp")) if end_row else None
            latest_row = latest.get(sid, start)
            planned = self._safe_int(latest_row.get("planned_duration_minutes"),
                                     self._safe_int(start.get("planned_duration_minutes"), 0))
            actual_seconds = int((end_dt - start_dt).total_seconds()) if start_dt and end_dt else 0
            status = end_row.get("completion_status", "In progress") if end_row else "In progress"

            deviation_seconds = 0
            deviation_count = 0
            switch_count = 0
            allowed_seconds = 0
            app_seconds = {}
            event_rows = grouped.get(sid, [])
            for i, event in enumerate(event_rows):
                t0 = self._parse_dt(event.get("timestamp"))
                if not t0:
                    continue
                if i + 1 < len(event_rows):
                    t1 = self._parse_dt(event_rows[i + 1].get("timestamp"))
                else:
                    t1 = end_dt
                duration = max(0, int((t1 - t0).total_seconds())) if t1 and t1 > t0 else 0
                app = event.get("application", "Unknown") or "Unknown"
                app_seconds[app] = app_seconds.get(app, 0) + duration
                allowed = str(event.get("allowed", "")).lower() == "true"
                if allowed:
                    allowed_seconds += duration
                else:
                    deviation_count += 1
                    deviation_seconds += duration

                if i > 0 and event.get("application") != event_rows[i - 1].get("application"):
                    switch_count += 1

            # If no event was logged in a tiny session, actual time is still useful.
            if actual_seconds > 0 and not event_rows:
                allowed_seconds = actual_seconds
            adherence = (allowed_seconds / actual_seconds * 100) if actual_seconds else 0
            longest_allowed = self._longest_allowed_run(event_rows, end_dt)
            top_deviation_app = self._top_deviation_app(event_rows, end_dt)
            records.append({
                "id": sid,
                "title": start.get("title") or start.get("goal") or "Untitled session",
                "goal": start.get("goal", ""),
                "apps": [a.strip() for a in start.get("allowed_apps", "").split("|") if a.strip()],
                "start": start_dt, "end": end_dt,
                "planned": planned,
                "actual_seconds": max(0, actual_seconds),
                "status": status,
                "deviations": deviation_count,
                "deviation_seconds": max(0, deviation_seconds),
                "adherence": max(0, min(100, adherence)),
                "switches": switch_count,
                "longest_focus_seconds": longest_allowed,
                "top_deviation_app": top_deviation_app,
            })
        records.sort(key=lambda r: r["start"] or datetime.min, reverse=True)
        return records

    def _longest_allowed_run(self, events, end_dt):
        longest = 0
        for i, event in enumerate(events):
            if str(event.get("allowed", "")).lower() != "true":
                continue
            t0 = self._parse_dt(event.get("timestamp"))
            t1 = self._parse_dt(events[i + 1].get("timestamp")) if i + 1 < len(events) else end_dt
            if t0 and t1 and t1 > t0:
                longest = max(longest, int((t1 - t0).total_seconds()))
        return longest

    def _top_deviation_app(self, events, end_dt):
        totals = {}
        for i, event in enumerate(events):
            if str(event.get("allowed", "")).lower() == "true":
                continue
            t0 = self._parse_dt(event.get("timestamp"))
            t1 = self._parse_dt(events[i + 1].get("timestamp")) if i + 1 < len(events) else end_dt
            if t0 and t1 and t1 > t0:
                app = event.get("application", "Unknown") or "Unknown"
                totals[app] = totals.get(app, 0) + int((t1 - t0).total_seconds())
        return max(totals, key=totals.get) if totals else "None"

    @staticmethod
    def _parse_dt(value):
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_int(value, default=0):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _fmt_seconds(seconds):
        seconds = max(0, int(seconds))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

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
        status_color = SUCCESS if record["status"] == "Completed" else WARNING if record["status"] == "In progress" else SECONDARY
        tk.Label(top, text=record["status"], bg=CARD_ALT, fg=status_color,
                 font=(FONT, 8, "bold"), padx=10, pady=5).pack(side="right")
        arrow = tk.Label(top, text="›", bg=CARD, fg=MUTED, font=(FONT, 16))
        arrow.pack(side="right", padx=(12, 0))

        summary = tk.Frame(card, bg=CARD)
        summary.pack(fill="x", padx=18, pady=(0, 14))
        self._metric_inline(summary, "Planned", f"{record['planned']} min")
        self._metric_inline(summary, "Actual", self._fmt_seconds(record["actual_seconds"]))
        self._metric_inline(summary, "Zone adherence", f"{record['adherence']:.0f}%")
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
            ("Most time outside zone", record["top_deviation_app"]),
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
        if not r["actual_seconds"]:
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

        total = len(records)
        completed = sum(r["status"] == "Completed" for r in records)
        actual = sum(r["actual_seconds"] for r in records)
        planned = sum(r["planned"] * 60 for r in records)
        deviations = sum(r["deviations"] for r in records)
        outside = sum(r["deviation_seconds"] for r in records)
        adherence = sum(r["adherence"] for r in records) / total
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
                self._fmt_seconds(actual),
                f"{self._fmt_seconds(planned)} planned across all sessions"
            ),
            (
                "Zone adherence",
                f"{adherence:.0f}%",
                "share of recorded time inside your declared zone"
            ),
            (
                "Completion rate",
                f"{completed / total * 100:.0f}%",
                f"{completed} of {total} sessions completed"
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

        self._bar_chart(
            left,
            records[-8:],
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
            records
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
                value = float(r.get(field, 0))
                label = r["title"][:20]
                canvas.create_text(0 + 2, y + 10, text=label, fill=SECONDARY,
                                   font=(FONT, 8), anchor="w")
                bar_w = max(2, (w - left - right) * min(1, value / maximum))
                canvas.create_rectangle(left, y + 3, left + bar_w, y + 17,
                                        fill=BLUE, outline="")
                canvas.create_text(left + bar_w + 7, y + 10, text=f"{value:.0f}{suffix}",
                                   fill=TEXT, font=(FONT, 8, "bold"), anchor="w")
        canvas.bind("<Configure>", draw)

    def _deviation_chart(self, parent, records):
        totals = {}
        for r in records:
            app = r["top_deviation_app"]
            if app != "None":
                totals[app] = totals.get(app, 0) + r["deviation_seconds"]
        ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:7]
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
        if adherence >= 90:
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
        self._header("Workspace analytics", "See the bigger picture",
                     "This tab is the bridge to the main multimodal workspace dashboard you are building separately.")
        card = self._card(self.content, padx=42, pady=0)
        tk.Label(card, text="Workspace Dashboard", bg=CARD, fg=TEXT,
                 font=(FONT, 16, "bold")).pack(anchor="w", padx=24, pady=(24, 7))
        tk.Label(card, text="Focus Assistant will eventually pass its session insights into the broader workspace analytics flow. The existing 29-column workspace schema remains unchanged.",
                 bg=CARD, fg=SECONDARY, font=(FONT, 9), wraplength=820,
                 justify="left").pack(anchor="w", padx=24)
        features = tk.Frame(card, bg=CARD)
        features.pack(fill="x", padx=24, pady=20)
        for title, desc in [
            ("Workspace behavior", "Applications, sessions, switching and activity patterns."),
            ("Behavior labels", "Connect focus behavior with the broader behavioral analytics pipeline."),
            ("Trends & anomalies", "Explore longer-term changes beyond individual focus sessions."),
        ]:
            f = tk.Frame(features, bg=CARD_ALT)
            f.pack(side="left", fill="both", expand=True, padx=(0, 8))
            tk.Label(f, text=title, bg=CARD_ALT, fg=TEXT,
                     font=(FONT, 9, "bold")).pack(anchor="w", padx=14, pady=(14, 5))
            tk.Label(f, text=desc, bg=CARD_ALT, fg=SECONDARY, font=(FONT, 7),
                     wraplength=210, justify="left").pack(anchor="w", padx=14, pady=(0, 14))
        RoundedButton(card, "Open Workspace Dashboard", self._open_dashboard,
                      width=210, primary=True, bg=CARD).pack(anchor="w", padx=24, pady=(0, 24))

    def _open_dashboard(self):
        webbrowser.open("http://localhost:5173")

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
        self.timer_window.destroy()
        self.deviation_popup.destroy()
        self.destroy()


def launch_focus_assistant():
    app = FocusAssistant()
    app.mainloop()


if __name__ == "__main__":
    launch_focus_assistant()
