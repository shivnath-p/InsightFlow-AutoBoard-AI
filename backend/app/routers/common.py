import json
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import AnalysisRun, Dashboard, Dataset, User


def owned_dataset(db: Session, user: User, dataset_id: str) -> Dataset:
    """Every lookup filters by org. A foreign id returns 404, never 403 (no existence leak)."""
    x = db.get(Dataset, dataset_id)
    if not x or x.org_id != user.org_id:
        raise HTTPException(404, "Dataset not found")
    return x


def owned_run(db: Session, user: User, run_id: str) -> AnalysisRun:
    x = db.get(AnalysisRun, run_id)
    if not x or x.org_id != user.org_id:
        raise HTTPException(404, "Run not found")
    return x


def owned_dashboard(db: Session, user: User, dashboard_id: str) -> Dashboard:
    x = db.get(Dashboard, dashboard_id)
    if not x or x.org_id != user.org_id:
        raise HTTPException(404, "Dashboard not found")
    return x


def ts(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def run_to_dict(r: AnalysisRun) -> dict:
    return {"id": r.id, "dataset_id": r.dataset_id, "question": r.question, "status": r.status,
            "plan": json.loads(r.plan), "facts": json.loads(r.facts),
            "claims": json.loads(r.claims), "warnings": json.loads(r.warnings),
            "trace": json.loads(r.trace), "clarification": r.clarification,
            "created_at": ts(r.created_at)}
