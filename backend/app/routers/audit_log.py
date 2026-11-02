from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AuditEvent, User
from ..security import get_current_user

router = APIRouter(prefix="/audit", tags=["Audit"])


@router.get("")
def list_events(limit: int = 100, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    rows = (db.query(AuditEvent).filter(AuditEvent.org_id == user.org_id)
            .order_by(AuditEvent.id.desc()).limit(max(1, min(limit, 500))).all())
    return [{"id": r.id, "action": r.action, "target_type": r.target_type,
             "target_id": r.target_id, "user_id": r.user_id, "detail": r.detail,
             "created_at": r.created_at.isoformat()} for r in rows]
