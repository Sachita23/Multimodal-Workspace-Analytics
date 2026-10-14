import sys
from pathlib import Path

from flask import Flask, Response, jsonify


# ---------------------------------------------------------
# Project paths
# ---------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import API_HOST, API_PORT
from team.api import init_team

PROCESSED_DATA_FILE = (
    PROJECT_ROOT
    / "data"
    / "processed"
    / "workspace_activity_processed.csv"
)


# ---------------------------------------------------------
# Flask application
# ---------------------------------------------------------

app = Flask(__name__)

init_team(app)

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


# ---------------------------------------------------------
# Health check
# ---------------------------------------------------------

@app.get("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "processed_file_exists": PROCESSED_DATA_FILE.exists(),
    })


# ---------------------------------------------------------
# Analytics CSV endpoint
# ---------------------------------------------------------

@app.get("/api/analytics.csv")
def analytics_csv():

    if not PROCESSED_DATA_FILE.exists():
        return jsonify({
            "error": "Processed analytics data is not available yet."
        }), 404

    csv_text = PROCESSED_DATA_FILE.read_text(
        encoding="utf-8"
    )

    response = Response(
        csv_text,
        mimetype="text/csv"
    )

    # Prevent browser/proxy caching.
    # The live pipeline continuously updates the CSV.
    response.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, max-age=0"
    )

    return response


# ---------------------------------------------------------
# Run server
# ---------------------------------------------------------

if __name__ == "__main__":

    # Flask's debugger allows running code from the browser, so it
    # is only enabled when the server is reachable from this machine.
    debug = API_HOST in LOOPBACK_HOSTS

    base_url = f"http://{API_HOST}:{API_PORT}"

    print("=" * 60)
    print("Workspace Analytics API")
    print("=" * 60)

    print("Processed file:")
    print(PROCESSED_DATA_FILE)

    print()
    print(f"API:        {base_url}")
    print(f"Health:     {base_url}/api/health")
    print(f"Analytics:  {base_url}/api/analytics.csv")
    print(f"Team:       {base_url}/api/team/...")

    print()
    print("Team admin key (paste into the Team page):")
    print(app.config["TEAM_ADMIN_KEY"])

    if not debug:
        print()
        print("Listening beyond this machine: debug mode is OFF.")
        print("Members' data travels over plain HTTP. Use a trusted")
        print("network, or put the server behind HTTPS.")

    print("=" * 60)

    app.run(
        host=API_HOST,
        port=API_PORT,
        debug=debug
    )
