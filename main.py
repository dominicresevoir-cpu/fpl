"""
Run this before each gameweek deadline:

    python main.py

Reads config from .env (copy .env.example -> .env and fill it in).
Writes reports/gw<N>_report.md and prints it to the console.

The actual pipeline lives in core.py, shared with the web app (app.py) —
this is just the CLI entry point.
"""

from dotenv import load_dotenv

from fpl_client import deadline_countdown
from core import FPLConfig, generate_report, save_report


def main():
    load_dotenv()
    config = FPLConfig.from_env()
    result = generate_report(config)

    print(f"Next gameweek: {result.gameweek_name} — deadline in "
          f"{deadline_countdown({'deadline_time': result.deadline})}")
    for warning in result.warnings:
        print(f"⚠️  {warning}")

    out_path = save_report(result)

    print("\n" + result.report_markdown)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    main()
