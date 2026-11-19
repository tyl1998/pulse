"""Export canonical schemas to stdout for the offline Node generation/check command."""

from __future__ import annotations

import json
from typing import Any

from pulse_schema.models import (
    CONTRACT_STATUS,
    CONTRACT_VERSION,
    DOCUMENT_ADAPTER,
    DOCUMENT_MODELS,
    JSON_SCHEMA_DIALECT,
    SCHEMA_PREFIX,
)


def schemas() -> dict[str, dict[str, Any]]:
    document = DOCUMENT_ADAPTER.json_schema(by_alias=True)
    document.update(
        {
            "$schema": JSON_SCHEMA_DIALECT,
            "$id": SCHEMA_PREFIX + "document",
            "title": "PulseDocument",
            "$comment": "Generated offline v1 candidate. Draft, not a frozen execution DSL.",
        }
    )
    result = {"document": document}
    for kind, model in DOCUMENT_MODELS.items():
        # The same definitions serve every file kind; no hand-maintained schema fragments.
        result[kind] = {
            "$schema": JSON_SCHEMA_DIALECT,
            "$id": SCHEMA_PREFIX + kind,
            "$comment": document["$comment"],
            "$ref": f"#/$defs/{model.__name__}",
            "$defs": document["$defs"],
        }
    return result


def main() -> None:
    print(
        json.dumps(
            {"version": CONTRACT_VERSION, "status": CONTRACT_STATUS, "schemas": schemas()},
            sort_keys=True,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
