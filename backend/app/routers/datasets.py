import json
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from .. import config, ingestion
from ..audit import log_event
from ..database import get_db
from ..models import AnalysisRun, Dashboard, DashboardVersion, Dataset, User
from ..schemas import DatasetUpdate
from ..security import get_current_user, heavy_limiter
from .common import owned_dataset, ts

router = APIRouter(prefix="/datasets", tags=["Datasets"])
CHUNK = 1024 * 1024
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f<>\"'`\\/]")


def _display_name(filename: str | None) -> str:
    """Uploaded names are shown in the UI and audit log: keep only printable characters."""
    return _UNSAFE_NAME.sub("", Path(filename or "dataset").name).strip() or "dataset"


def _brief(x: Dataset) -> dict:
    return {"id": x.id, "name": x.name, "filename": x.filename, "description": x.description,
            "row_count": x.row_count, "created_at": ts(x.created_at)}


@router.post("/upload", status_code=201)
def upload(file: UploadFile = File(...), db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    # sync def on purpose: file IO runs in FastAPI's threadpool instead of blocking the loop
    heavy_limiter.check("upload:" + user.id)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ingestion.ALLOWED_EXTENSIONS:
        raise HTTPException(400, "Only .csv and .xlsx files are supported")
    if db.query(Dataset).filter(Dataset.org_id == user.org_id).count() >= config.MAX_DATASETS_PER_ORG:
        raise HTTPException(409, f"Dataset limit reached ({config.MAX_DATASETS_PER_ORG}). "
                                 "Delete an old dataset first.")
    did = uuid.uuid4().hex
    folder = config.UPLOAD_DIR / user.org_id / did
    folder.mkdir(parents=True, exist_ok=True)
    raw, parquet = folder / f"raw{ext}", folder / "data.parquet"
    try:
        size = 0
        with raw.open("wb") as out:
            while chunk := file.file.read(CHUNK):
                size += len(chunk)
                if size > ingestion.MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"File larger than {config.MAX_UPLOAD_MB} MB")
                out.write(chunk)
        try:
            profile = ingestion.ingest(raw, ext, parquet)
        except ingestion.IngestionError as exc:
            raise HTTPException(422, str(exc))
        shown = _display_name(file.filename)
        item = Dataset(id=did, org_id=user.org_id, owner_id=user.id,
                       name=Path(shown).stem[:120] or "dataset", filename=shown[:200],
                       raw_path=str(raw), parquet_path=str(parquet), row_count=profile["rows"],
                       profile=json.dumps(profile))
        db.add(item)
        db.commit()
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)  # no orphan files on any failure
        raise
    log_event(db, user, "dataset.upload", "dataset", did,
              {"filename": item.filename, "bytes": size, "rows": profile["rows"]})
    return {"dataset_id": did, "filename": item.filename, "profile": profile}


@router.get("")
def list_all(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(Dataset).filter(Dataset.org_id == user.org_id).order_by(
        Dataset.created_at.desc()).all()
    return [_brief(x) for x in rows]


@router.get("/{dataset_id}")
def get_one(dataset_id: str, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    x = owned_dataset(db, user, dataset_id)
    return {**_brief(x), "profile": json.loads(x.profile)}


@router.put("/{dataset_id}")
def update(dataset_id: str, payload: DatasetUpdate, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    x = owned_dataset(db, user, dataset_id)
    if payload.name is not None:
        x.name = payload.name
    if payload.description is not None:
        x.description = payload.description
    db.commit()
    log_event(db, user, "dataset.update", "dataset", x.id)
    return {"message": "Dataset updated"}


@router.delete("/{dataset_id}")
def delete(dataset_id: str, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    x = owned_dataset(db, user, dataset_id)
    dash_ids = [d.id for d in db.query(Dashboard).filter(Dashboard.dataset_id == x.id)]
    if dash_ids:
        db.query(DashboardVersion).filter(DashboardVersion.dashboard_id.in_(dash_ids)).delete(
            synchronize_session=False)
        db.query(Dashboard).filter(Dashboard.id.in_(dash_ids)).delete(synchronize_session=False)
    db.query(AnalysisRun).filter(AnalysisRun.dataset_id == x.id).delete(synchronize_session=False)
    folder = Path(x.parquet_path).parent
    db.delete(x)
    db.commit()
    shutil.rmtree(folder, ignore_errors=True)
    log_event(db, user, "dataset.delete", "dataset", dataset_id,
              {"dashboards_removed": len(dash_ids)})
    return {"message": "Dataset deleted"}


@router.get("/{dataset_id}/insights")
def get_insights(dataset_id: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    heavy_limiter.check("insights:" + user.id)
    x = owned_dataset(db, user, dataset_id)
    return ingestion.insights(x.parquet_path)
