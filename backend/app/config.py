"""Central configuration. Every value comes from environment variables (see .env.example)."""
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger("insightflow")

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --- storage -------------------------------------------------------------------------------
# The DB lives inside DATA_DIR so the Docker volume keeps it across rebuilds.
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'insightflow.db').as_posix()}")

# --- security ------------------------------------------------------------------------------
# ENVIRONMENT=production turns on strict start-up checks and hides the interactive API docs.
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT in ("production", "prod")
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    if IS_PRODUCTION:
        raise RuntimeError("SECRET_KEY must be set when ENVIRONMENT=production")
    SECRET_KEY = secrets.token_urlsafe(48)
    log.warning("SECRET_KEY not set: using a random key. All logins reset on restart.")
elif len(SECRET_KEY) < 32:
    if IS_PRODUCTION:
        raise RuntimeError("SECRET_KEY must be at least 32 characters")
    log.warning("SECRET_KEY is shorter than 32 characters; use a long random value.")
ACCESS_TOKEN_MINUTES = _int("ACCESS_TOKEN_MINUTES", 60)
ALLOW_REGISTRATION = _bool("ALLOW_REGISTRATION", True)
PBKDF2_ITERATIONS = _int("PBKDF2_ITERATIONS", 600_000)
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
                if o.strip()]
ASK_RATE_LIMIT_PER_MIN = _int("ASK_RATE_LIMIT_PER_MIN", 20)
LOGIN_RATE_LIMIT_PER_MIN = _int("LOGIN_RATE_LIMIT_PER_MIN", 10)
HEAVY_RATE_LIMIT_PER_MIN = _int("HEAVY_RATE_LIMIT_PER_MIN", 30)  # uploads, auto dashboards, exports
MAX_JSON_BODY_BYTES = _int("MAX_JSON_BODY_BYTES", 256 * 1024)  # every non-upload request body
ENABLE_DOCS = _bool("ENABLE_DOCS", not IS_PRODUCTION)
ENABLE_HSTS = _bool("ENABLE_HSTS", IS_PRODUCTION)  # only turn on when served over HTTPS

# --- uploads -------------------------------------------------------------------------------
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 100)
MAX_COLUMNS = _int("MAX_COLUMNS", 500)
MAX_ROWS = _int("MAX_ROWS", 2_000_000)
MAX_UNZIPPED_MB = _int("MAX_UNZIPPED_MB", 500)      # .xlsx is a zip: guard against zip bombs
MAX_DATASETS_PER_ORG = _int("MAX_DATASETS_PER_ORG", 50)
ALLOWED_EXTENSIONS = {".csv", ".xlsx"}

# --- analytics engine ----------------------------------------------------------------------
QUERY_TIMEOUT_SECONDS = _int("QUERY_TIMEOUT_SECONDS", 20)
DUCKDB_MEMORY_LIMIT = os.getenv("DUCKDB_MEMORY_LIMIT", "1GB")
DUCKDB_THREADS = _int("DUCKDB_THREADS", 2)

# --- agent / LLM ---------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
LLM_TIMEOUT_SECONDS = _int("LLM_TIMEOUT_SECONDS", 30)
LLM_MAX_OUTPUT_TOKENS = _int("LLM_MAX_OUTPUT_TOKENS", 1024)
LLM_MAX_PROMPT_CHARS = _int("LLM_MAX_PROMPT_CHARS", 12000)
# The planner only sees column names. The narrator also sees aggregated group labels and values.
# Set to false to keep every result on this server (the deterministic narrator is used instead).
LLM_SHARE_RESULTS = _bool("LLM_SHARE_RESULTS", True)
MAX_AGENT_STEPS = _int("MAX_AGENT_STEPS", 12)
