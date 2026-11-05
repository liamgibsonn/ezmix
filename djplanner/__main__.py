"""
Start the app:   python -m djplanner
Then open the address it prints (http://127.0.0.1:8000 by default) in your browser.

The server restarts itself whenever a .py file in djplanner/ is saved (e.g. new
phrase-detection rules), so refreshing the page shows the changes. Tracks get
re-analysed with the new rules the first time they're shown, which can take a few
seconds each. Add --no-reload to turn the auto-restart off.
"""

import argparse

import uvicorn

from . import config


def main() -> None:
    ap = argparse.ArgumentParser(description="Start the DJ Set Planner.")
    ap.add_argument("--no-reload", action="store_true",
                    help="don't restart the server when the code changes")
    args = ap.parse_args()

    print(f"DJ Set Planner running at http://{config.HOST}:{config.PORT}  (Ctrl+C to stop)")
    uvicorn.run(
        "djplanner.api:app", host=config.HOST, port=config.PORT, log_level="warning",
        reload=not args.no_reload,
        # Only watch our own code, not .cache/ or the saved plans.
        reload_dirs=[str(config.PROJECT_DIR / "djplanner")],
    )


if __name__ == "__main__":
    main()
