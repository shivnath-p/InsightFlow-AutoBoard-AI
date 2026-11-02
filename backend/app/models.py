from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    org_id: Mapped[str] = mapped_column(String, index=True)
    role: Mapped[str] = mapped_column(String, default="analyst")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String)
    filename: Mapped[str] = mapped_column(String)
    raw_path: Mapped[str] = mapped_column(String)
    parquet_path: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile: Mapped[str] = mapped_column(Text)  # JSON
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnalysisRun(Base):
    """One agent run. Facts/claims are stored as a snapshot so exports are reproducible."""
    __tablename__ = "analysis_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    user_id: Mapped[str] = mapped_column(String)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String)  # done | partial | needs_input | failed
    plan: Mapped[str] = mapped_column(Text, default="{}")
    facts: Mapped[str] = mapped_column(Text, default="[]")
    claims: Mapped[str] = mapped_column(Text, default="[]")
    warnings: Mapped[str] = mapped_column(Text, default="[]")
    trace: Mapped[str] = mapped_column(Text, default="[]")
    clarification: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Dashboard(Base):
    __tablename__ = "dashboards"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    spec: Mapped[str] = mapped_column(Text)  # JSON, validated by DashboardSpec
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class DashboardVersion(Base):
    __tablename__ = "dashboard_versions"
    __table_args__ = (UniqueConstraint("dashboard_id", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dashboard_id: Mapped[str] = mapped_column(ForeignKey("dashboards.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String)
    spec: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, index=True)
    target_type: Mapped[str | None] = mapped_column(String, nullable=True)
    target_id: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
