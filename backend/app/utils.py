import json
import math
from datetime import date, datetime
from typing import Any

import numpy as np


def json_safe(obj: Any) -> Any:
    """Make pandas/numpy output JSON-compliant: NaN/inf -> None, numpy -> python."""
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


def dumps(obj: Any) -> str:
    return json.dumps(json_safe(obj), ensure_ascii=False)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None
