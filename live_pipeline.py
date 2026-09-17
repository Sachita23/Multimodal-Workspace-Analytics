import time
from datetime import datetime

from run_pipeline import build_dashboard_dataset


# ============================================================
# CONFIGURATION
# ============================================================

PIPELINE_INTERVAL = 10


# ============================================================
# RUN ONCE
# ============================================================

def run_once():

    print("\n" + "=" * 70)
    print(
        f"LIVE UPDATE: "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print("=" * 70)

    try:

        df = build_dashboard_dataset()

        print(
            f"\nLive dataset updated: "
            f"{len(df)} rows"
        )

    except Exception as error:

        print(
            "\nPipeline error:"
        )

        print(error)


# ============================================================
# MAIN LOOP
# ============================================================

def main():

    print("=" * 70)
    print("REAL-TIME WORKSPACE ANALYTICS PIPELINE")
    print("=" * 70)

    print(
        f"\nPipeline refresh interval: "
        f"{PIPELINE_INTERVAL} seconds"
    )

    print(
        "\nPress CTRL+C to stop."
    )

    try:

        while True:

            run_once()

            print(
                f"\nWaiting {PIPELINE_INTERVAL} seconds..."
            )

            time.sleep(
                PIPELINE_INTERVAL
            )

    except KeyboardInterrupt:

        print("\n")
        print("=" * 70)
        print("LIVE PIPELINE STOPPED")
        print("=" * 70)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()