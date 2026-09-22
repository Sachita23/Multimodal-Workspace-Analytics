from pathlib import Path

from flask import Flask, Response, jsonify


# ---------------------------------------------------------
# Project paths
# ---------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

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

    print("=" * 60)
    print("Workspace Analytics API")
    print("=" * 60)

    print(f"Processed file:")
    print(PROCESSED_DATA_FILE)

    print()
    print("API:")
    print("http://127.0.0.1:5000")

    print()
    print("Health:")
    print("http://127.0.0.1:5000/api/health")

    print()
    print("Analytics:")
    print("http://127.0.0.1:5000/api/analytics.csv")

    print("=" * 60)

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )