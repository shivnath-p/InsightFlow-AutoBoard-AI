"""Hardening tests added in the security review (headers, limits, upload guards, auth)."""
import io
import json
import os
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import agent, config, llm, security
from app.database import SessionLocal
from app.main import app
from app.models import DashboardVersion
from app.verify import check_text
from .conftest import SALES, ask, csv_bytes, upload

BACKEND_DIR = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- response headers
def test_security_headers_on_api_but_docs_stay_usable(client):
    h = client.get("/health").headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["cross-origin-resource-policy"] == "same-site"
    assert "content-security-policy" not in client.get("/docs").headers  # Swagger needs scripts


def test_unhandled_error_is_generic_and_still_has_headers(alice, dataset, monkeypatch):
    def boom(_path):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr("app.routers.datasets.ingestion.insights", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get(f"/datasets/{dataset}/insights", headers=alice)
    assert r.status_code == 500 and r.json() == {"detail": "Internal server error"}
    assert "secret" not in r.text
    assert r.headers["x-content-type-options"] == "nosniff"


# ---------------------------------------------------------------- request body limits
def test_oversized_json_body_is_rejected_by_content_length(client):
    body = json.dumps({"email": "a@b.co", "password": "x" * (config.MAX_JSON_BODY_BYTES + 10)})
    r = client.post("/auth/login", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_oversized_chunked_body_is_rejected_without_content_length(client):
    def chunks():
        for _ in range(config.MAX_JSON_BODY_BYTES // 1000 + 5):
            yield b" " * 1000
    r = client.post("/auth/login", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_upload_body_limit_follows_the_file_limit(client, alice, monkeypatch):
    monkeypatch.setattr("app.main.MAX_UPLOAD_BYTES", 10)
    big = b"a,b\n" + b"1,2\n" * 400_000  # > 10 bytes + 1 MiB multipart allowance
    assert upload(client, alice, big).status_code == 413


# ---------------------------------------------------------------- JWT
def _token(**claims):
    return jwt.encode(claims, config.SECRET_KEY, algorithm="HS256")


def test_token_without_exp_or_with_odd_sub_is_rejected(client, alice):
    me = client.get("/auth/me", headers=alice).json()["id"]
    exp = datetime.now(timezone.utc) + timedelta(minutes=5)
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {_token(sub=me)}"}
                      ).status_code == 401  # never expires -> refused
    for sub in (["x"], 123, None):
        t = _token(sub=sub, exp=exp)
        assert client.get("/auth/me", headers={"Authorization": f"Bearer {t}"}).status_code == 401
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {_token(sub=me, exp=exp)}"}
                      ).status_code == 200


def test_expired_and_wrong_key_tokens_are_rejected(client, alice):
    me = client.get("/auth/me", headers=alice).json()["id"]
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {_token(sub=me, exp=past)}"}
                      ).status_code == 401
    forged = jwt.encode({"sub": me, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                        "x" * 40, algorithm="HS256")
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


# ---------------------------------------------------------------- rate limiting
def test_rate_limiter_forgets_idle_keys():
    rl = security.RateLimiter(limit=100_000, window_seconds=1)
    for i in range(50):
        rl.check(f"ip-{i}")
    assert len(rl.hits) == 50
    time.sleep(1.1)
    for _ in range(rl._SWEEP_EVERY):
        rl.check("active")
        if len(rl.hits) == 1:
            break
    assert list(rl.hits) == ["active"]  # the 50 idle keys were swept, memory stays bounded


def test_login_is_limited_per_account_not_only_per_ip(client, monkeypatch):
    monkeypatch.setattr("app.routers.auth.account_limiter", security.RateLimiter(3))
    email = f"victim-{uuid.uuid4().hex[:6]}@example.com"
    codes = [client.post("/auth/login", json={"email": email, "password": "wrong-pass-123"}
                         ).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    other = client.post("/auth/login", json={"email": "someone-else@example.com",
                                             "password": "wrong-pass-123"})
    assert other.status_code == 401  # a different account is unaffected


def test_429_tells_the_client_when_to_retry(client, monkeypatch):
    monkeypatch.setattr("app.routers.auth.account_limiter", security.RateLimiter(1))
    body = {"email": "retry@example.com", "password": "wrong-pass-123"}
    client.post("/auth/login", json=body)
    r = client.post("/auth/login", json=body)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"


def test_heavy_endpoints_are_rate_limited(client, alice, dataset, monkeypatch):
    monkeypatch.setattr(security.heavy_limiter, "limit", 1)
    assert client.post(f"/dashboards/auto/{dataset}", headers=alice).status_code == 201
    assert client.post(f"/dashboards/auto/{dataset}", headers=alice).status_code == 429
    monkeypatch.setattr(security.heavy_limiter, "limit", 1000)


# ---------------------------------------------------------------- upload guards
def _zip_bomb(megabytes: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/big.bin", b"\0" * megabytes * 1024 * 1024)
    return buf.getvalue()


def test_zip_bomb_xlsx_is_refused_before_parsing(client, alice, monkeypatch):
    monkeypatch.setattr(config, "MAX_UNZIPPED_MB", 10)
    bomb = _zip_bomb(40)
    assert len(bomb) < 200_000  # tiny on disk, huge when unpacked
    r = upload(client, alice, bomb, "bomb.xlsx")
    assert r.status_code == 422 and "unpacked" in r.json()["detail"]


def test_file_content_must_match_extension(client, alice):
    assert upload(client, alice, b"just text, not a zip", "a.xlsx").status_code == 422
    assert upload(client, alice, _zip_bomb(1), "a.csv").status_code == 422   # zip renamed .csv
    assert upload(client, alice, b"a,b\n1,\x00\n", "bin.csv").status_code == 422  # NUL bytes


def test_row_limit(client, alice, monkeypatch):
    monkeypatch.setattr(config, "MAX_ROWS", 3)
    r = upload(client, alice, csv_bytes())
    assert r.status_code == 422 and "Too many rows" in r.json()["detail"]
    buf = io.BytesIO()
    SALES.to_excel(buf, index=False)
    assert upload(client, alice, buf.getvalue(), "s.xlsx").status_code == 422


def test_column_limit_checked_from_header_only(client, alice, monkeypatch):
    monkeypatch.setattr(config, "MAX_COLUMNS", 3)
    r = upload(client, alice, csv_bytes())
    assert r.status_code == 422 and "Too many columns" in r.json()["detail"]


@pytest.mark.parametrize("sep", [";", "\t", "|"])
def test_other_csv_delimiters_still_work(client, alice, sep):
    data = SALES.to_csv(index=False, sep=sep).encode()
    r = upload(client, alice, data)
    assert r.status_code == 201 and r.json()["profile"]["column_count"] == 5


def test_dataset_quota_per_org(client, alice, monkeypatch):
    monkeypatch.setattr(config, "MAX_DATASETS_PER_ORG", 1)
    assert upload(client, alice).status_code == 201
    assert upload(client, alice).status_code == 409


def test_uploaded_filename_is_sanitised(client, alice):
    r = upload(client, alice, csv_bytes(), name='q1<img src=x onerror=1>"sales\x07.csv')
    assert r.status_code == 201
    name = r.json()["filename"]
    assert not any(ch in name for ch in '<>"\x07/\\')


# ---------------------------------------------------------------- LLM output hardening
def test_links_and_markup_are_not_allowed_in_findings(client):
    fact = {"fact_id": "F1", "groups": 1, "source_rows": 6,
            "rows": [{"labels": {"region": "North"}, "value": 800.0}]}
    for text in ("North has 800. See https://evil.example/login",
                 "North has 800 [click](javascript:alert(1))",
                 "North has 800 <img src=x onerror=alert(1)>",
                 "North has 800 www.evil.example",
                 "North " + "very " * 100 + "large 800"):
        assert check_text(text, [fact]), text
    assert check_text("North has 800", [fact]) == []


def test_results_can_be_kept_off_the_llm(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "key")
    monkeypatch.setattr(config, "LLM_SHARE_RESULTS", False)

    def must_not_run(*_a, **_k):
        raise AssertionError("aggregated results were sent to the LLM")
    monkeypatch.setattr(llm, "generate_json", must_not_run)
    fact = {"fact_id": "F1", "description": "SUM of sales", "params": {
            "dimensions": [], "time_column": None, "order": "desc"},
            "rows": [{"labels": {}, "value": 1200.0}], "groups": 1, "source_rows": 6}
    claims = agent.narrate(agent.Run(), "total sales?", [fact], [])
    assert "1,200" in claims[0]["text"]


def test_a_crashing_recompute_drops_the_fact_instead_of_failing_the_run(
        client, alice, dataset, monkeypatch):
    def boom(*_a):
        raise ValueError("bad timestamp")
    monkeypatch.setattr(agent, "recompute_fact", boom)
    run = ask(client, alice, dataset, "What is the total sales?")
    assert run["status"] == "failed" and run["facts"] == []
    assert any("verification failed" in w for w in run["warnings"])


def test_oversized_filter_value_is_rejected_by_schema():
    from pydantic import ValidationError
    from app.schemas import MetricRequest
    with pytest.raises(ValidationError):
        MetricRequest(metric="sales", agg="sum", filters=[
            {"column": "region", "op": "=", "value": "x" * 5000}])


# ---------------------------------------------------------------- dashboards
def test_concurrent_save_loses_with_409_not_500(client, alice, dataset):
    run = ask(client, alice, dataset, "Which product has the highest total sales?")
    did = client.post(f"/dashboards/from-run/{run['id']}", headers=alice, json={}).json()["id"]
    spec = client.get(f"/dashboards/{did}", headers=alice).json()["spec"]
    # another writer already took version 2 (the dashboard row still says 1)
    with SessionLocal() as db:
        db.add(DashboardVersion(dashboard_id=did, version=2, name="other", spec="{}",
                                created_by="someone"))
        db.commit()
    r = client.put(f"/dashboards/{did}", headers=alice,
                   json={"name": "mine", "spec": spec, "base_version": 1})
    assert r.status_code == 409
    assert client.get(f"/dashboards/{did}", headers=alice).json()["version"] == 1  # rolled back


# ---------------------------------------------------------------- production config
def _import_config(**env):
    clean = {k: v for k, v in os.environ.items() if k not in ("SECRET_KEY", "ENVIRONMENT")}
    return subprocess.run([sys.executable, "-c", "import app.config as c; print(c.ENABLE_DOCS)"],
                          cwd=BACKEND_DIR, env={**clean, **env}, capture_output=True, text=True)


def test_production_refuses_missing_or_short_secret_key():
    missing = _import_config(ENVIRONMENT="production", DATA_DIR="/tmp/if-prod-test")
    assert missing.returncode != 0 and "SECRET_KEY must be set" in missing.stderr
    short = _import_config(ENVIRONMENT="production", SECRET_KEY="short", DATA_DIR="/tmp/if-prod-test")
    assert short.returncode != 0 and "at least 32" in short.stderr


def test_production_hides_api_docs_by_default():
    ok = _import_config(ENVIRONMENT="production", SECRET_KEY="k" * 40, DATA_DIR="/tmp/if-prod-test")
    assert ok.returncode == 0 and ok.stdout.strip() == "False"


# ---------------------------------------------------------------- dashboard export
@pytest.mark.parametrize("fmt,magic", [("csv", b"\xef\xbb\xbf"), ("xlsx", b"PK"),
                                       ("pdf", b"%PDF"), ("pptx", b"PK")])
def test_dashboard_export_all_formats(client, alice, bob, dataset, fmt, magic):
    did = client.post(f"/dashboards/auto/{dataset}", headers=alice).json()["id"]
    r = client.get(f"/dashboards/{did}/export/{fmt}", headers=alice)
    assert r.status_code == 200 and r.content.startswith(magic)
    assert client.get(f"/dashboards/{did}/export/{fmt}", headers=bob).status_code == 404
    assert client.get(f"/dashboards/{did}/export/exe", headers=alice).status_code == 400
    d = client.get(f"/dashboards/{did}", headers=alice).json()
    assert any("records" in c["text"] for c in d["claims"])  # real findings, not placeholder text
