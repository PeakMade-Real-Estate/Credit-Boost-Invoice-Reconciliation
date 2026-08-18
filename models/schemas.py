"""
JSON serialisation helpers for ReconciliationResult.

All model objects use ``Decimal`` for currency and Python ``date``/``datetime``
for dates – neither of which is serialisable by the default ``json`` module.
This module provides a custom encoder and convenience functions so that results
can be persisted to disk and round-tripped back into model objects.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class ReconciliationJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder that handles Decimal, date, datetime, and Enum."""

    def default(self, obj: Any) -> Any:  # noqa: ANN401
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        if isinstance(obj, Enum):
            return obj.value
        return super().default(obj)


def result_to_json(result: Any) -> str:
    """Serialise any dataclass (or nested structure) to a JSON string."""
    return json.dumps(asdict(result), cls=ReconciliationJSONEncoder, indent=2)


def result_to_dict(result: Any) -> dict:
    """Serialise any dataclass to a plain dict (Decimal → str, dates → ISO)."""
    raw = asdict(result)
    return json.loads(json.dumps(raw, cls=ReconciliationJSONEncoder))
