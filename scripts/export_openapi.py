"""Write the API contract to docs/openapi.json.

    python -m scripts.export_openapi

A test fails if the committed file is out of date, so the contract in the repository
always matches the code.
"""

import json
from pathlib import Path

from app.main import create_app

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def render() -> str:
    return json.dumps(create_app().openapi(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    OUTPUT.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT}")
