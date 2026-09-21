"""
config.py — every setting in one place.

Values come from environment variables. A `.env` file in the project root is
loaded automatically (copy `.env.example` to `.env` and edit it). A variable
that is already set in the real environment wins over `.env`, so cloud
platforms can inject secrets without touching files.

Settings are read at call time (functions, not constants), so a test or the
API can change os.environ and the next call sees it.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # project root (where .env lives)


# ---------------------------------------------------------------------------
# .env loading (tiny parser: KEY=value, # comments, optional quotes/export)
# ---------------------------------------------------------------------------
def load_dotenv(path=None):
    path = Path(path) if path else ROOT / ".env"
    if not path.is_file():
        return False
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:                      # inline comment on an unquoted value
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)       # real environment wins
    return True


load_dotenv()


def _get(name, default=None):
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _flag(name, default=False):
    value = _get(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _path(name, default):
    """Relative paths are relative to the project root, not the current folder."""
    p = Path(_get(name, default))
    return p if p.is_absolute() else ROOT / p


# ---------------------------------------------------------------------------
# AI (any OpenAI-compatible chat API: Groq by default)
# ---------------------------------------------------------------------------
def llm_api_key():
    return _get("LLM_API_KEY") or _get("GROQ_API_KEY")


def llm_base_url():
    return _get("LLM_BASE_URL", "https://api.groq.com/openai/v1")


def llm_model():
    return _get("LLM_MODEL", "openai/gpt-oss-120b")


def llm_vision_model():
    return _get("LLM_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")


def ai_disabled():
    """Switched off on purpose (SDOC_DISABLE_AI=1 or `--no-ai`)."""
    return _flag("SDOC_DISABLE_AI")


# ---------------------------------------------------------------------------
# Data + outputs
# ---------------------------------------------------------------------------
def data_source():
    """A data folder (inbox/ + attachments/) or the organizers' server URL."""
    value = _get("SDOC_DATA", "data_v2")
    if value.startswith(("http://", "https://")):
        return value.rstrip("/")
    return str(_path("SDOC_DATA", "data_v2"))


def data_is_remote():
    return data_source().startswith(("http://", "https://"))


def out_dir():
    return _path("SDOC_OUT", "output")


def ground_truth_path():
    """Only for local scoring; None if there isn't one."""
    explicit = _get("SDOC_GROUND_TRUTH")
    if explicit:
        return _path("SDOC_GROUND_TRUTH", explicit)
    if not data_is_remote():
        candidate = Path(data_source()) / "ground_truth.json"
        if candidate.is_file():
            return candidate
    return None


def cache_dir():
    return _path("SDOC_CACHE_DIR", ".sdoc_cache")


def cache_enabled():
    return not _flag("SDOC_NO_CACHE")


# ---------------------------------------------------------------------------
# Pipeline behaviour
# ---------------------------------------------------------------------------
def extraction_mode():
    """ai: AI reads PDF/DOCX/XLSX/scans, rules cross-check (default)
    auto: rules first, AI only for gaps      rules: never call the AI"""
    mode = (_get("SDOC_EXTRACTION_MODE", "ai") or "ai").lower()
    return mode if mode in ("ai", "auto", "rules") else "ai"


def ocr_policy():
    """review: scans always go to a human with a proposal (default)
    trust: compare scans automatically when OCR confidence is high"""
    policy = (_get("SDOC_OCR_POLICY", "review") or "review").lower()
    return policy if policy in ("review", "trust") else "review"


def ocr_trust_min():
    try:
        return float(_get("SDOC_OCR_TRUST_MIN", "0.95"))
    except ValueError:
        return 0.95


# ---------------------------------------------------------------------------
# Web API
# ---------------------------------------------------------------------------
def api_host():
    return _get("SDOC_API_HOST", "127.0.0.1")


def api_port():
    """PORT (set by hosting platforms like Render / Railway / Cloud Run) wins."""
    try:
        return int(_get("PORT") or _get("SDOC_API_PORT", "8000"))
    except ValueError:
        return 8000


def cors_origins():
    return [o.strip() for o in _get("SDOC_CORS", "http://localhost:5173").split(",") if o.strip()]


# ---------------------------------------------------------------------------
# Public deployment guardrails
# ---------------------------------------------------------------------------
def seed_dir():
    """Results pre-computed at image build time (see Dockerfile). If present and
    there are no results yet, they're copied in at startup: instant dashboard,
    no AI quota spent on boot."""
    value = _get("SDOC_SEED_DIR")
    return _path("SDOC_SEED_DIR", value) if value else None


def autorun():
    """Process the inbox on startup if there are no results yet, so a fresh
    deploy opens with a populated dashboard."""
    return _flag("SDOC_AUTORUN")


def admin_token():
    """If set, starting a full pipeline run needs this token (X-Admin-Token)."""
    return _get("SDOC_ADMIN_TOKEN")


def run_cooldown_seconds():
    """Minimum gap between full runs started from the web (0 = no limit)."""
    try:
        return max(0, int(_get("SDOC_RUN_COOLDOWN", "0")))
    except ValueError:
        return 0


def max_upload_mb():
    try:
        return max(1, int(_get("SDOC_MAX_UPLOAD_MB", "10")))
    except ValueError:
        return 10


def checks_per_hour():
    """Upload & Check requests allowed per visitor IP per hour (each can call the AI)."""
    try:
        return max(1, int(_get("SDOC_CHECKS_PER_HOUR", "30")))
    except ValueError:
        return 30


def max_batch_mb():
    """Total size of one batch upload (after unzipping)."""
    try:
        return max(1, int(_get("SDOC_MAX_BATCH_MB", "60")))
    except ValueError:
        return 60


def max_batch_emails():
    try:
        return max(1, int(_get("SDOC_MAX_BATCH_EMAILS", "1000")))
    except ValueError:
        return 1000


def batches_per_hour():
    """Batch uploads allowed per visitor IP per hour."""
    try:
        return max(1, int(_get("SDOC_BATCHES_PER_HOUR", "6")))
    except ValueError:
        return 6


def uploads_dir():
    return out_dir() / "uploads"


# ---------------------------------------------------------------------------
def describe():
    """Safe summary for logs / the health endpoint. Never includes the key."""
    key = llm_api_key()
    return {
        "data": data_source(),
        "output": str(out_dir()),
        "ai": "off (SDOC_DISABLE_AI)" if ai_disabled()
        else ("on" if key else "off (LLM_API_KEY not set)"),
        "llm_base_url": llm_base_url(),
        "llm_model": llm_model(),
        "llm_vision_model": llm_vision_model(),
        "llm_api_key": f"set (…{key[-4:]})" if key and len(key) > 8 else ("set" if key else "missing"),
        "extraction_mode": extraction_mode(),
        "ocr_policy": ocr_policy(),
        "env_file": str(ROOT / ".env") if (ROOT / ".env").is_file() else "none (using defaults)",
    }
