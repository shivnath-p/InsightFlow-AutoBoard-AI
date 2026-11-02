import hashlib
import json
import uuid

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit import log_event
from ..bi import advanced_insights, executive_summary
from ..database import get_db
from ..models import AnalysisRun, Dashboard, DashboardVersion, User
from ..schemas import (Component, DashboardFromRun, DashboardIn, DashboardSpec,
                       DashboardUpdate, Layout, RestoreIn)
from ..security import get_current_user, heavy_limiter
from .common import owned_dashboard, owned_dataset, owned_run, ts
from .runs import EXPORTS

router = APIRouter(prefix="/dashboards", tags=["Dashboards"])


def _check_bindings(db: Session, user: User, dataset_id: str, spec: DashboardSpec) -> dict:
    """Every component must be bound to a fact of a run in the same org and dataset."""
    runs: dict[str, dict] = {}
    for c in spec.components:
        if c.run_id not in runs:
            run = owned_run(db, user, c.run_id)
            if run.dataset_id != dataset_id:
                raise HTTPException(422, f"Component {c.id}: run belongs to another dataset")
            runs[c.run_id] = {f["fact_id"]: f for f in json.loads(run.facts)}
        if c.fact_id not in runs[c.run_id]:
            raise HTTPException(422, f"Component {c.id}: unknown fact {c.fact_id}")
    return runs


def _add_version(db: Session, d: Dashboard, user: User) -> None:
    db.add(DashboardVersion(dashboard_id=d.id, version=d.version, name=d.name, spec=d.spec,
                            created_by=user.id))


def _commit_new_version(db: Session) -> None:
    """Two saves based on the same version race on UNIQUE(dashboard_id, version): the loser
    gets a clean 409 instead of a 500."""
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Dashboard was changed by someone else, reload and retry")


def _claims(db: Session, user: User, spec: DashboardSpec) -> list[dict]:
    """Saved findings of the runs behind this dashboard. A finding is kept only if every fact it
    cites is still on the dashboard (findings without evidence ids are the dataset overview)."""
    shown: dict[str, set[str]] = {}
    for c in spec.components:
        shown.setdefault(c.run_id, set()).add(c.fact_id)
    out = []
    for run_id, fact_ids in shown.items():
        for claim in json.loads(owned_run(db, user, run_id).claims):
            if set(claim.get("fact_ids", [])) <= fact_ids:
                out.append(claim)
    return out


def _detail(db: Session, user: User, d: Dashboard) -> dict:
    spec = DashboardSpec(**json.loads(d.spec))
    runs = _check_bindings(db, user, d.dataset_id, spec)
    data = {}
    for c in spec.components:  # data comes from the saved fact snapshot, not live queries
        data[f"{c.run_id}:{c.fact_id}"] = runs[c.run_id][c.fact_id]
    return {"id": d.id, "name": d.name, "dataset_id": d.dataset_id, "version": d.version,
            "spec": spec.model_dump(), "data": data, "claims": _claims(db, user, spec),
            "updated_at": ts(d.updated_at)}


def _auto_fact(fid: str, description: str, rows: list[dict], chart: str, params: dict) -> dict:
    return {"fact_id": fid, "tool": "auto_dashboard", "description": description,
            "params": params, "rows": rows, "groups": len(rows), "source_rows": 0,
            "verified": True, "query_hash": hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16],
            "chart": chart}


def _build_auto_run(ds):
    """Deterministic, data-aware dashboard analysis. Never invents business metrics."""
    df = pd.read_parquet(ds.parquet_path)
    profile = json.loads(ds.profile)
    facts, claims, fid = [], [], 1
    def add(desc, rows, chart, params):
        nonlocal fid
        f = _auto_fact(f"A{fid}", desc, rows, chart, params); facts.append(f); fid += 1
        return f
    # universal row count KPI
    add("Total records", [{"labels": {}, "value": int(len(df))}], "kpi", {"metric": "rows", "agg": "count"})
    numeric = [c["name"] for c in profile.get("columns", []) if c.get("kind") == "numeric"][:3]
    categorical = [c["name"] for c in profile.get("columns", []) if c.get("kind") in ("categorical", "text")][:3]
    dates = [c["name"] for c in profile.get("columns", []) if c.get("kind") == "datetime"][:1]
    for col in numeric:
        vals = pd.to_numeric(df[col], errors="coerce")
        if vals.notna().any():
            total = float(vals.sum())
            add(f"Total {col}", [{"labels": {}, "value": total}], "kpi", {"metric": col, "agg": "sum"})
            if categorical:
                g = df.assign(__v=vals).groupby(categorical[0], dropna=False)["__v"].sum().sort_values(ascending=False).head(10)
                rows=[{"labels": {categorical[0]: "(missing)" if pd.isna(k) else str(k)}, "value": float(v)} for k,v in g.items()]
                if len(rows)>1: add(f"{col} by {categorical[0]}", rows, "bar", {"metric":col,"by":categorical[0],"agg":"sum"})
            break
    if dates and numeric:
        dcol, ncol = dates[0], numeric[0]
        tmp=df[[dcol,ncol]].dropna().copy()
        if not tmp.empty:
            tmp[dcol]=pd.to_datetime(tmp[dcol], errors="coerce"); tmp=tmp.dropna(subset=[dcol])
            g=tmp.groupby(tmp[dcol].dt.to_period("M"))[ncol].sum().tail(24)
            rows=[{"labels": {f"{dcol}_month": str(k.start_time.date())}, "value": float(v)} for k,v in g.items()]
            if len(rows)>1: add(f"Monthly trend of {ncol}", rows, "line", {"metric":ncol,"time":dcol,"grain":"month","agg":"sum"})
    if categorical:
        c=categorical[0]; counts=df[c].fillna("(missing)").astype(str).value_counts().head(10)
        rows=[{"labels": {c:k}, "value": int(v)} for k,v in counts.items()]
        if len(rows)>1: add(f"Record distribution by {c}", rows, "pie", {"metric":"rows","by":c,"agg":"count"})
    # Advanced BI: deterministic anomaly, trend and concentration analysis.
    extra, advanced_claims, warnings = advanced_insights(df, numeric, dates, categorical)
    for desc, rows, chart, params in extra:
        f=add(desc, rows, chart, params)
        # bind newly computed evidence ids to the matching advanced finding when possible
        for claim in advanced_claims:
            if not claim["fact_ids"] and (params.get("column") in claim["text"]): claim["fact_ids"]=[f["fact_id"]]
    summary = executive_summary(df, facts, profile)
    claims.extend({"text":x,"fact_ids":[]} for x in summary)
    claims.extend(advanced_claims)
    if len(facts) <= 1:
        claims.append({"text":"Not enough supported numeric, category, or date fields for a richer automatic dashboard.","fact_ids":[facts[0]["fact_id"]]})
    else:
        claims.append({"text":f"Auto Dashboard selected {len(facts)} evidence-backed components based on the available dataset structure.","fact_ids":[f["fact_id"] for f in facts]})
    return {"status":"done","facts":facts,"claims":claims,"warnings":warnings,"trace":[{"step":1,"name":"profile","detail":"inspected dataset columns and types"},{"step":2,"name":"select","detail":"selected only data-supported KPIs and visuals"},{"step":3,"name":"advanced_bi","detail":"computed supported trend, anomaly and concentration signals"}]}


@router.post("/auto/{dataset_id}", status_code=201)
def create_auto_dashboard(dataset_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    heavy_limiter.check("auto:" + user.id)  # reads the whole dataset, so it is rate limited
    ds = owned_dataset(db, user, dataset_id)
    result = _build_auto_run(ds)
    run = AnalysisRun(id=uuid.uuid4().hex, org_id=user.org_id, user_id=user.id, dataset_id=ds.id,
                      question="Generate Auto Dashboard", status=result["status"], plan="{}",
                      facts=json.dumps(result["facts"]), claims=json.dumps(result["claims"]),
                      warnings=json.dumps(result["warnings"]), trace=json.dumps(result["trace"]), clarification=None)
    db.add(run); db.flush()
    comps=[]; y=0
    for f in result["facts"]:
        typ=f["chart"]
        if typ=="kpi": layout=Layout(x=(len([c for c in comps if c.type=="kpi"])%4)*3,y=0,w=3,h=2)
        else:
            nonk=len([c for c in comps if c.type!="kpi"]); layout=Layout(x=(nonk%2)*6,y=2+(nonk//2)*4,w=6,h=4)
        comps.append(Component(id=f"c{len(comps)+1}",type=typ,title=f["description"][:160],run_id=run.id,fact_id=f["fact_id"],layout=layout))
    spec=DashboardSpec(components=comps)
    d=Dashboard(id=uuid.uuid4().hex,org_id=user.org_id,dataset_id=ds.id,name=f"Auto Dashboard - {ds.name}"[:120],spec=spec.model_dump_json(),version=1,created_by=user.id)
    db.add(d); db.flush(); _add_version(db,d,user); db.commit()
    log_event(db,user,"dashboard.auto_create","dashboard",d.id,{"facts":len(result["facts"])})
    return {"id":d.id,"run_id":run.id,"message":"Auto Dashboard created","components":len(comps)}


@router.post("", status_code=201)
def create(p: DashboardIn, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    owned_dataset(db, user, p.dataset_id)
    _check_bindings(db, user, p.dataset_id, p.spec)
    d = Dashboard(id=uuid.uuid4().hex, org_id=user.org_id, dataset_id=p.dataset_id, name=p.name,
                  spec=p.spec.model_dump_json(), version=1, created_by=user.id)
    db.add(d)
    db.flush()
    _add_version(db, d, user)
    db.commit()
    log_event(db, user, "dashboard.create", "dashboard", d.id)
    return {"id": d.id, "version": 1, "message": "Dashboard created"}


@router.post("/from-run/{run_id}", status_code=201)
def create_from_run(run_id: str, p: DashboardFromRun, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    """AutoBoard: build a dashboard deterministically from the verified facts of a run."""
    run = owned_run(db, user, run_id)
    facts = json.loads(run.facts)
    if not facts:
        raise HTTPException(409, "This run has no verified facts")
    comps, y = [], 0
    kpis = [f for f in facts if f["chart"] == "kpi"]
    charts = [f for f in facts if f["chart"] != "kpi"]
    for i, f in enumerate(kpis):
        comps.append(Component(id=f"c{len(comps) + 1}", type="kpi", title=f["description"][:160],
                               run_id=run.id, fact_id=f["fact_id"],
                               layout=Layout(x=(i % 4) * 3, y=y + (i // 4) * 2, w=3, h=2)))
    y += ((len(kpis) + 3) // 4) * 2
    for i, f in enumerate(charts):
        comps.append(Component(id=f"c{len(comps) + 1}", type=f["chart"],
                               title=f["description"][:160], run_id=run.id, fact_id=f["fact_id"],
                               layout=Layout(x=(i % 2) * 6, y=y + (i // 2) * 4, w=6, h=4)))
    spec = DashboardSpec(components=comps)
    name = p.name or run.question[:80]
    d = Dashboard(id=uuid.uuid4().hex, org_id=user.org_id, dataset_id=run.dataset_id, name=name,
                  spec=spec.model_dump_json(), version=1, created_by=user.id)
    db.add(d)
    db.flush()
    _add_version(db, d, user)
    db.commit()
    log_event(db, user, "dashboard.create", "dashboard", d.id, {"from_run": run.id})
    return {"id": d.id, "version": 1, "message": "Dashboard created"}


@router.get("")
def list_all(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.query(Dashboard).filter(Dashboard.org_id == user.org_id).order_by(
        Dashboard.updated_at.desc()).all()
    return [{"id": d.id, "name": d.name, "dataset_id": d.dataset_id, "version": d.version,
             "updated_at": ts(d.updated_at)} for d in rows]


@router.get("/{dashboard_id}")
def get_one(dashboard_id: str, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    return _detail(db, user, owned_dashboard(db, user, dashboard_id))


@router.put("/{dashboard_id}")
def update(dashboard_id: str, p: DashboardUpdate, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    d = owned_dashboard(db, user, dashboard_id)
    if p.base_version != d.version:
        raise HTTPException(409, f"Dashboard changed (current version is {d.version})")
    _check_bindings(db, user, d.dataset_id, p.spec)
    d.name, d.spec, d.version = p.name, p.spec.model_dump_json(), d.version + 1
    _add_version(db, d, user)
    _commit_new_version(db)
    log_event(db, user, "dashboard.update", "dashboard", d.id, {"version": d.version})
    return {"version": d.version, "message": "Dashboard updated"}


@router.get("/{dashboard_id}/export/{fmt}")
def export_dashboard(dashboard_id: str, fmt: str, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    """Export exactly what the dashboard shows (saved facts + findings) as pdf/pptx/xlsx/csv."""
    if fmt not in EXPORTS:
        raise HTTPException(400, "Format must be csv, xlsx, pdf or pptx")
    heavy_limiter.check("export:" + user.id)
    d = owned_dashboard(db, user, dashboard_id)
    detail = _detail(db, user, d)
    facts, used = [], set()
    for c in detail["spec"]["components"]:
        fact = dict(detail["data"][f"{c['run_id']}:{c['fact_id']}"])
        if fact["fact_id"] in used:  # same id from two runs: keep sheet names/evidence unique
            fact["fact_id"] = f"{fact['fact_id']}_{len(used)}"
        used.add(fact["fact_id"])
        fact["description"] = c["title"]  # the title the user chose on the dashboard
        facts.append(fact)
    if not facts:
        raise HTTPException(409, "This dashboard has no components to export")
    snapshot = {"id": d.id, "question": d.name, "status": "done", "created_at": ts(d.updated_at),
                "facts": facts, "claims": detail["claims"], "warnings": []}
    mime, render = EXPORTS[fmt]
    log_event(db, user, "dashboard.export", "dashboard", d.id, {"format": fmt})
    return Response(render(snapshot), media_type=mime, headers={
        "Content-Disposition": f'attachment; filename="insightflow-dashboard-{d.id[:8]}.{fmt}"'})


@router.get("/{dashboard_id}/versions")
def versions(dashboard_id: str, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    d = owned_dashboard(db, user, dashboard_id)
    rows = db.query(DashboardVersion).filter(DashboardVersion.dashboard_id == d.id).order_by(
        DashboardVersion.version.desc()).all()
    return [{"version": v.version, "name": v.name, "created_at": ts(v.created_at)}
            for v in rows]


@router.post("/{dashboard_id}/restore")
def restore(dashboard_id: str, p: RestoreIn, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    """Undo = restore an older version as a NEW version, so history is never rewritten."""
    d = owned_dashboard(db, user, dashboard_id)
    old = db.query(DashboardVersion).filter(DashboardVersion.dashboard_id == d.id,
                                            DashboardVersion.version == p.version).first()
    if not old:
        raise HTTPException(404, "Version not found")
    d.name, d.spec, d.version = old.name, old.spec, d.version + 1
    _add_version(db, d, user)
    _commit_new_version(db)
    log_event(db, user, "dashboard.restore", "dashboard", d.id,
              {"restored_from": p.version, "version": d.version})
    return {"version": d.version, "message": f"Restored version {p.version}"}


@router.delete("/{dashboard_id}")
def delete(dashboard_id: str, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    d = owned_dashboard(db, user, dashboard_id)
    db.query(DashboardVersion).filter(DashboardVersion.dashboard_id == d.id).delete()
    db.delete(d)
    db.commit()
    log_event(db, user, "dashboard.delete", "dashboard", dashboard_id)
    return {"message": "Dashboard deleted"}
