import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import exports
from ..agent import run_agent
from ..audit import log_event
from ..database import get_db
from ..models import AnalysisRun, User
from ..schemas import AskIn
from ..security import ask_limiter, get_current_user, heavy_limiter
from ..utils import dumps
from .common import owned_dataset, owned_run, run_to_dict

router = APIRouter(tags=["AI Analytics"])
EXPORTS = {"csv": ("text/csv; charset=utf-8", exports.to_csv),
           "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    exports.to_xlsx),
           "pdf": ("application/pdf", exports.to_pdf),
           "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", exports.to_pptx)}


@router.post("/ask")
def ask(p: AskIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ask_limiter.check(user.id)
    ds = owned_dataset(db, user, p.dataset_id)  # scope is injected by the backend, not the LLM
    result = run_agent(p.question.strip(), json.loads(ds.profile), ds.parquet_path)
    run = AnalysisRun(
        id=uuid.uuid4().hex, org_id=user.org_id, user_id=user.id, dataset_id=ds.id,
        question=p.question.strip(), status=result["status"], plan=dumps(result["plan"]),
        facts=dumps(result["facts"]), claims=dumps(result["claims"]),
        warnings=dumps(result["warnings"]), trace=dumps(result["trace"]),
        clarification=result["clarification"])
    db.add(run)
    db.commit()
    log_event(db, user, "run.create", "run", run.id,
              {"status": run.status, "facts": len(result["facts"]),
               "llm_egress": result["egress"]})  # what was sent to the model (sizes only)
    out = run_to_dict(run)
    out["options"] = result.get("options", [])
    return {"run": out}


@router.get("/runs")
def list_runs(dataset_id: str | None = None, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    q = db.query(AnalysisRun).filter(AnalysisRun.org_id == user.org_id)
    if dataset_id:
        q = q.filter(AnalysisRun.dataset_id == dataset_id)
    return [{"id": r.id, "question": r.question, "status": r.status,
             "created_at": r.created_at.isoformat()}
            for r in q.order_by(AnalysisRun.created_at.desc()).limit(50)]


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return {"run": run_to_dict(owned_run(db, user, run_id))}


@router.get("/runs/{run_id}/export/{fmt}")
def export_run(run_id: str, fmt: str, request: Request, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    if fmt not in EXPORTS:
        raise HTTPException(400, "Format must be csv, xlsx, pdf or pptx")
    heavy_limiter.check("export:" + user.id)
    run = run_to_dict(owned_run(db, user, run_id))  # re-authorised here, at job time
    if not run["facts"]:
        raise HTTPException(409, "This run has no verified facts to export")
    mime, render = EXPORTS[fmt]
    log_event(db, user, "run.export", "run", run_id, {"format": fmt})
    return Response(render(run), media_type=mime, headers={
        "Content-Disposition": f'attachment; filename="insightflow-{run_id[:8]}.{fmt}"'})
