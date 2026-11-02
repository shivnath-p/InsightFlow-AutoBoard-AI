import json
import logging

from sqlalchemy.orm import Session

from .models import AuditEvent, User

log = logging.getLogger("insightflow.audit")


def log_event(db: Session, user: User | None, action: str, target_type: str | None = None,
              target_id: str | None = None, detail: dict | None = None, org_id: str | None = None):
    """Append-only audit trail. Never put secrets or raw data in `detail`."""
    try:
        db.add(AuditEvent(
            org_id=user.org_id if user else org_id,
            user_id=user.id if user else None,
            action=action, target_type=target_type, target_id=target_id,
            detail=json.dumps(detail, ensure_ascii=False) if detail else None))
        db.commit()
    except Exception:  # auditing must never break the request
        db.rollback()
        log.exception("failed to write audit event %s", action)
