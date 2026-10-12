"""Minimal live Jev demo.

Add your Jev API key to settings.json, then run:
    .venv/bin/python tests/jev_demo.py
"""

from __future__ import annotations

import json
from pathlib import Path

from typesafe_sdk import Choice, TypeSafeClient


SETTINGS_PATH = Path(__file__).resolve().parents[1] / "settings.json"


def main() -> None:
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    jev = settings["jev"]
    if not jev.get("api_key"):
        raise SystemExit("Set jev.api_key in settings.json before running this demo.")

    with TypeSafeClient(
        api_key=jev["api_key"],
        model=jev.get("model", "jev-latest"),
    ) as client:
        response = client.system_one(
            state=(
                "Hi, I've been trying to connect my Stripe account for 3 days "
                "and the integration keeps failing. I'm losing sales."
            ),
            questions={
                "department": Choice(
                    instructions="Which team should handle this",
                    criteria={
                        "billing": "Payment or subscription issues",
                        "technical": "Bugs or integration problems",
                        "sales": "Pricing or account questions",
                    },
                )
            },
        )

    # Print the API's complete JSON shape, including model, answers, and usage.
    print(json.dumps(response.raw_http_response.json(), indent=2))


if __name__ == "__main__":
    main()
