import csv
import uuid
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FOCUS_DATA_DIR = PROJECT_ROOT / "data" / "focus"
FOCUS_SESSIONS_FILE = FOCUS_DATA_DIR / "focus_sessions.csv"
FOCUS_EVENTS_FILE = FOCUS_DATA_DIR / "focus_events.csv"

SESSION_FIELDS = [
    "session_id", "event_type", "timestamp", "title", "goal",
    "planned_duration_minutes", "allowed_apps", "completion_status"
]
EVENT_FIELDS = [
    "timestamp", "focus_session_id", "title", "goal", "application",
    "window_title", "allowed", "status", "remaining_seconds"
]


class FocusManager:
    """Stores focus-session intent and app-level deviations.

    Browser/tab context is deliberately not mixed into this layer yet. A later
    browser-context monitor can feed richer context into the same evaluator.
    """

    def __init__(self):
        self.session_id = None
        self.title = ""
        self.goal = ""
        self.duration_minutes = 0
        self.allowed_apps = []
        self.started_at = None
        self.expected_end_at = None
        self.running = False
        self.last_application = None
        self.last_window_title = None
        self.last_compliance_status = None
        FOCUS_DATA_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def normalize_app_name(value):
        if not value:
            return ""
        return str(value).strip().lower()

    def start_session(self, title, goal, duration_minutes, allowed_apps):
        if self.running:
            raise RuntimeError("A focus session is already running.")

        title = str(title).strip()
        goal = str(goal).strip()
        if not title:
            raise ValueError("Please enter a focus session title.")
        if not goal:
            raise ValueError("Please enter what you want to focus on.")

        duration_minutes = int(duration_minutes)
        if duration_minutes <= 0:
            raise ValueError("Focus duration must be greater than zero.")

        cleaned_apps = []
        existing = set()
        for app in allowed_apps:
            app = str(app).strip()
            normalized = self.normalize_app_name(app)
            if app and normalized not in existing:
                cleaned_apps.append(app)
                existing.add(normalized)

        if not cleaned_apps:
            raise ValueError("Please select at least one application.")

        self.session_id = str(uuid.uuid4())
        self.title = title
        self.goal = goal
        self.duration_minutes = duration_minutes
        self.allowed_apps = cleaned_apps
        self.started_at = datetime.now()
        self.expected_end_at = self.started_at + timedelta(minutes=duration_minutes)
        self.running = True
        self.last_application = None
        self.last_window_title = None
        self.last_compliance_status = None

        self._write_session_start()
        return self.session_id

    def remaining_seconds(self):
        if not self.running or not self.expected_end_at:
            return 0
        remaining = (self.expected_end_at - datetime.now()).total_seconds()
        return max(0, int(remaining))

    def is_application_allowed(self, application, window_title=""):
        if not self.running:
            return True

        app_normalized = self.normalize_app_name(application)
        title_normalized = self.normalize_app_name(window_title)

        # The Focus Assistant owns a few helper windows (main app, timer and
        # deviation overlay). Interacting with those windows must never count
        # as a user deviation.
        internal_windows = ("focus assistant", "focus timer", "focus deviation")
        if any(marker in title_normalized for marker in internal_windows):
            return True

        for allowed in self.allowed_apps:
            allowed_normalized = self.normalize_app_name(allowed)
            if not allowed_normalized:
                continue
            if (
                allowed_normalized in app_normalized
                or app_normalized in allowed_normalized
            ):
                return True
        return False

    def evaluate_application(self, application, window_title=""):
        if not self.running:
            return {"allowed": True, "status": "No active focus session"}

        allowed = self.is_application_allowed(application, window_title)
        status = "Within focus zone" if allowed else "Focus deviation"

        changed = (
            application != self.last_application
            or window_title != self.last_window_title
            or status != self.last_compliance_status
        )

        if changed:
            self._write_focus_event(application, window_title, allowed, status)

        self.last_application = application
        self.last_window_title = window_title
        self.last_compliance_status = status

        return {
            "allowed": allowed,
            "status": status,
            "application": application,
            "window_title": window_title,
        }

    def allow_application(self, application):
        application = str(application).strip()
        if not application:
            return
        normalized = self.normalize_app_name(application)
        existing = {self.normalize_app_name(app) for app in self.allowed_apps}
        if normalized not in existing:
            self.allowed_apps.append(application)
            self._write_focus_event(
                application,
                "",
                True,
                "Application added to focus zone",
            )

    def stop_session(self, completion_status="Stopped"):
        if not self.running:
            return
        stopped_at = datetime.now()
        self._write_session_end(stopped_at, completion_status)
        self.running = False

    def is_due(self):
        return bool(self.running and self.remaining_seconds() <= 0)

    def extend_session(self, additional_minutes):
        if not self.running:
            raise RuntimeError("There is no active focus session to extend.")
        additional_minutes = int(additional_minutes)
        if additional_minutes <= 0:
            raise ValueError("Additional focus time must be greater than zero.")

        self.duration_minutes += additional_minutes
        self.expected_end_at = datetime.now() + timedelta(minutes=additional_minutes)
        self._write_session_event(
            "EXTEND",
            completion_status=f"+{additional_minutes} minutes",
        )
        return self.remaining_seconds()

    @staticmethod
    def _append_csv(file_path, fieldnames, row):
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = file_path.exists() and file_path.stat().st_size > 0

        if not file_exists:
            with open(file_path, "w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow(row)
            return

        # Keep existing files usable if they were created by the earlier
        # prototype. New fields are added through a one-time schema migration.
        with open(file_path, "r", newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            existing_fields = reader.fieldnames or []
            rows = list(reader)

        if existing_fields == fieldnames:
            with open(file_path, "a", newline="", encoding="utf-8") as file:
                csv.DictWriter(file, fieldnames=fieldnames).writerow(row)
            return

        merged_fields = list(existing_fields)
        for field in fieldnames:
            if field not in merged_fields:
                merged_fields.append(field)

        temp_file = file_path.with_suffix(file_path.suffix + ".tmp")
        with open(temp_file, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=merged_fields)
            writer.writeheader()
            for old_row in rows:
                writer.writerow(old_row)
            writer.writerow(row)
        temp_file.replace(file_path)

    def _write_session_event(self, event_type, completion_status=""):
        self._append_csv(
            FOCUS_SESSIONS_FILE,
            SESSION_FIELDS,
            {
                "session_id": self.session_id,
                "event_type": event_type,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "title": self.title,
                "goal": self.goal,
                "planned_duration_minutes": self.duration_minutes,
                "allowed_apps": " | ".join(self.allowed_apps),
                "completion_status": completion_status,
            },
        )

    def _write_session_start(self):
        self._append_csv(
            FOCUS_SESSIONS_FILE,
            SESSION_FIELDS,
            {
                "session_id": self.session_id,
                "event_type": "START",
                "timestamp": self.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                "title": self.title,
                "goal": self.goal,
                "planned_duration_minutes": self.duration_minutes,
                "allowed_apps": " | ".join(self.allowed_apps),
                "completion_status": "",
            },
        )

    def _write_session_end(self, stopped_at, completion_status):
        self._append_csv(
            FOCUS_SESSIONS_FILE,
            SESSION_FIELDS,
            {
                "session_id": self.session_id,
                "event_type": "END",
                "timestamp": stopped_at.strftime("%Y-%m-%d %H:%M:%S"),
                "title": self.title,
                "goal": self.goal,
                "planned_duration_minutes": self.duration_minutes,
                "allowed_apps": " | ".join(self.allowed_apps),
                "completion_status": completion_status,
            },
        )

    def _write_focus_event(self, application, window_title, allowed, status):
        self._append_csv(
            FOCUS_EVENTS_FILE,
            EVENT_FIELDS,
            {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "focus_session_id": self.session_id,
                "title": self.title,
                "goal": self.goal,
                "application": application,
                "window_title": window_title,
                "allowed": bool(allowed),
                "status": status,
                "remaining_seconds": self.remaining_seconds(),
            },
        )
