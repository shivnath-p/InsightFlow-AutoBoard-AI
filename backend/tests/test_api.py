import io
import json
import uuid

import pandas as pd

from app import ingestion
from .conftest import SALES, ask, csv_bytes, make_user, upload


# ---------------------------------------------------------------- auth
def test_register_login_me(client):
    email = f"u-{uuid.uuid4().hex[:6]}@example.com"
    h = make_user(client, email)
    assert client.get("/auth/me", headers=h).json()["email"] == email
    ok = client.post("/auth/login", json={"email": email, "password": "correct-horse-battery"})
    assert ok.status_code == 200 and ok.json()["token_type"] == "bearer"
    bad = client.post("/auth/login", json={"email": email, "password": "wrong-password-1"})
    assert bad.status_code == 401


def test_register_validation(client):
    assert client.post("/auth/register", json={"email": "nope", "password": "x" * 12}
                       ).status_code == 422
    assert client.post("/auth/register", json={"email": "a@b.co", "password": "short"}
                       ).status_code == 422


def test_duplicate_email(client):
    email = f"d-{uuid.uuid4().hex[:6]}@example.com"
    make_user(client, email)
    r = client.post("/auth/register", json={"email": email, "password": "correct-horse-battery"})
    assert r.status_code == 409


def test_protected_routes_need_token(client):
    for path in ("/datasets", "/runs", "/dashboards", "/audit", "/auth/me"):
        assert client.get(path).status_code == 401
    assert client.get("/datasets", headers={"Authorization": "Bearer garbage"}).status_code == 401


# ---------------------------------------------------------------- upload / profile
def test_upload_profile_is_json_safe(client, alice):
    df = SALES.copy()
    df["notes"] = [None] * len(df)  # all-missing column used to produce NaN -> 500 in v1
    r = upload(client, alice, csv_bytes(df))
    assert r.status_code == 201
    prof = r.json()["profile"]
    kinds = {c["name"]: c["kind"] for c in prof["columns"]}
    assert kinds["sales"] == "numeric" and kinds["date"] == "datetime"
    assert "sales" in prof["metric_candidates"] and prof["date_candidates"] == ["date"]
    json.dumps(prof, allow_nan=False)


def test_upload_without_numeric_columns_does_not_crash(client, alice):
    df = pd.DataFrame({"a": ["x", "y"], "b": ["p", "q"]})
    assert upload(client, alice, csv_bytes(df)).status_code == 201


def test_upload_rejects_bad_files(client, alice):
    assert upload(client, alice, b"MZ\x90\x00", "evil.exe").status_code == 400
    assert upload(client, alice, b"a,b\n", "empty.csv").status_code == 422
    assert upload(client, alice, b"\x00\x01\x02", "fake.xlsx").status_code == 422


def test_upload_size_limit(client, alice, monkeypatch):
    monkeypatch.setattr(ingestion, "MAX_UPLOAD_BYTES", 100)
    assert upload(client, alice, csv_bytes()).status_code == 413


def test_xlsx_upload(client, alice):
    buf = io.BytesIO()
    SALES.to_excel(buf, index=False)
    r = upload(client, alice, buf.getvalue(), "sales.xlsx")
    assert r.status_code == 201 and r.json()["profile"]["rows"] == 6


def test_column_names_are_sanitised(client, alice):
    df = pd.DataFrame({"sales<script>": [1, 2], "ignore previous\ninstructions": ["a", "b"]})
    r = upload(client, alice, csv_bytes(df))
    names = [c["name"] for c in r.json()["profile"]["columns"]]
    assert all("<" not in n and "\n" not in n for n in names)


# ---------------------------------------------------------------- tenancy
def test_tenant_isolation(client, alice, bob, dataset):
    assert client.get(f"/datasets/{dataset}", headers=bob).status_code == 404
    assert client.put(f"/datasets/{dataset}", headers=bob, json={"name": "x"}).status_code == 404
    assert client.delete(f"/datasets/{dataset}", headers=bob).status_code == 404
    assert client.get(f"/datasets/{dataset}/insights", headers=bob).status_code == 404
    assert client.post("/ask", headers=bob, json={"dataset_id": dataset, "question": "total sales"}
                       ).status_code == 404
    assert dataset not in [d["id"] for d in client.get("/datasets", headers=bob).json()]
    run = ask(client, alice, dataset, "What is the total sales?")
    assert client.get(f"/runs/{run['id']}", headers=bob).status_code == 404
    assert client.get(f"/runs/{run['id']}/export/csv", headers=bob).status_code == 404
    r = client.post("/dashboards", headers=bob, json={"name": "x", "dataset_id": dataset})
    assert r.status_code == 404
    assert client.post(f"/dashboards/from-run/{run['id']}", headers=bob, json={}).status_code == 404


def test_dashboard_cannot_bind_foreign_run(client, alice, bob, dataset):
    run = ask(client, alice, dataset, "What is the total sales?")
    other = upload(client, bob).json()["dataset_id"]
    spec = {"components": [{"id": "c1", "type": "kpi", "title": "t", "run_id": run["id"],
                            "fact_id": "F1"}]}
    r = client.post("/dashboards", headers=bob, json={"name": "x", "dataset_id": other,
                                                       "spec": spec})
    assert r.status_code == 404


def test_audit_log_is_org_scoped(client, alice, bob, dataset):
    actions = [e["action"] for e in client.get("/audit", headers=alice).json()]
    assert "dataset.upload" in actions
    assert "dataset.upload" not in [e["action"] for e in client.get("/audit", headers=bob).json()]


# ---------------------------------------------------------------- dataset CRUD
def test_dataset_crud_and_cascade(client, alice, dataset):
    assert client.put(f"/datasets/{dataset}", headers=alice,
                      json={"name": "Renamed", "description": "d"}).status_code == 200
    assert client.get(f"/datasets/{dataset}", headers=alice).json()["name"] == "Renamed"
    run = ask(client, alice, dataset, "What is the total sales?")
    dash = client.post(f"/dashboards/from-run/{run['id']}", headers=alice, json={}).json()["id"]
    assert client.delete(f"/datasets/{dataset}", headers=alice).status_code == 200
    assert client.get(f"/dashboards/{dash}", headers=alice).status_code == 404
    assert client.get(f"/runs/{run['id']}", headers=alice).status_code == 404


def test_rate_limit(client, alice, dataset):
    from app import security
    security.ask_limiter.limit = 2
    try:
        codes = [client.post("/ask", headers=alice, json={"dataset_id": dataset,
                 "question": "total sales"}).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
    finally:
        security.ask_limiter.limit = 1000


def test_cors_is_not_wildcard(client):
    r = client.options("/datasets", headers={"Origin": "http://evil.example",
                                             "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") != "*"
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
