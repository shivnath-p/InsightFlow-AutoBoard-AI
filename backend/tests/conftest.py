import io
import os
import tempfile

# Environment must be set before the app is imported.
_tmp = tempfile.mkdtemp(prefix="insightflow-test-")
os.environ.update({"DATA_DIR": _tmp, "SECRET_KEY": "test-secret-key-" + "x" * 32,
                   "PBKDF2_ITERATIONS": "1000", "GEMINI_API_KEY": "",
                   "ASK_RATE_LIMIT_PER_MIN": "1000", "LOGIN_RATE_LIMIT_PER_MIN": "1000",
                   "HEAVY_RATE_LIMIT_PER_MIN": "1000"})

import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import security  # noqa: E402
from app.main import app  # noqa: E402

SALES = pd.DataFrame({
    "date": ["2024-01-05", "2024-01-20", "2024-02-03", "2024-02-18", "2024-03-02", "2024-03-25"],
    "region": ["North", "South", "North", "South", "North", "South"],
    "product": ["Widget", "Widget", "Gadget", "Gizmo", "Widget", "Gadget"],
    "sales": [100.0, 200.0, 300.0, 50.0, 400.0, 150.0],
    "units": [1, 2, 3, 1, 4, 2],
})


def csv_bytes(df: pd.DataFrame = SALES) -> bytes:
    return df.to_csv(index=False).encode()


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_limiters():
    security.ask_limiter.reset()
    security.login_limiter.reset()
    security.account_limiter.reset()
    security.heavy_limiter.reset()


def make_user(client, email):
    r = client.post("/auth/register", json={"email": email, "password": "correct-horse-battery"})
    assert r.status_code == 201, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


@pytest.fixture()
def alice(client):
    import uuid
    return make_user(client, f"alice-{uuid.uuid4().hex[:8]}@example.com")


@pytest.fixture()
def bob(client):
    import uuid
    return make_user(client, f"bob-{uuid.uuid4().hex[:8]}@example.com")


def upload(client, headers, data=None, name="sales.csv"):
    r = client.post("/datasets/upload", headers=headers,
                    files={"file": (name, io.BytesIO(data or csv_bytes()), "text/csv")})
    return r


@pytest.fixture()
def dataset(client, alice):
    r = upload(client, alice)
    assert r.status_code == 201, r.text
    return r.json()["dataset_id"]


def ask(client, headers, dataset_id, question):
    r = client.post("/ask", headers=headers, json={"dataset_id": dataset_id, "question": question})
    assert r.status_code == 200, r.text
    return r.json()["run"]
