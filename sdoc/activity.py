"""
activity.py — live "what is happening right now" for the web UI.

Every stage reports into this while it works, so a visitor always sees a
current step instead of a frozen button:

    classifying the email -> reading the SI (PDF) -> scanning with OCR ->
    the AI is reading the BL -> AI rate-limited, retrying in 12s -> comparing

Jobs: a full pipeline run ("run"), an uploaded batch (its batch id), or one
Upload & Check (an id the browser generates). The code doing the work binds
its job for the current thread; the stages just call step(...), which is a
no-op when nothing is bound (CLI runs, tests).

Modes (drive the colour/label in the UI):
  local    rule-based work (classifying, parsing text / PDF / Word / Excel)
  ocr      reading a scanned page
  ai       waiting on the AI model
  ai_wait  the AI provider is rate-limiting us; retrying after a countdown
  compare  comparing the 7 fields
"""
import threading
import time
from contextvars import ContextVar

from . import config

_lock = threading.Lock()
_jobs = {}
_current = ContextVar("sdoc_activity_job", default=None)
_ai = {"in_flight": 0, "calls": 0, "last_error": None, "last_error_at": None, "wait_until": None}
KEEP_FINISHED_SECONDS = 600
MAX_JOBS = 50


def start(job_id, kind, total=None, label=None, target=None):
    """target: what the job produces (e.g. the uploaded email's id), so a browser
    that reloaded mid-check can still find the result."""
    now = time.time()
    with _lock:
        _jobs[job_id] = {"job_id": job_id, "kind": kind, "label": label, "target": target, "started": now,
                         "updated": now, "finished": None, "error": None, "step": "Starting",
                         "detail": None, "mode": "local", "email": None, "done": 0, "total": total}
        if len(_jobs) > MAX_JOBS:                     # forget the oldest
            for old in sorted(_jobs, key=lambda j: _jobs[j]["started"])[:len(_jobs) - MAX_JOBS]:
                _jobs.pop(old, None)


def bind(job_id):
    """Attach the current thread's work to a job. Returns a token for unbind()."""
    return _current.set(job_id)


def unbind(token):
    _current.reset(token)


def _update(**fields):
    job_id = _current.get()
    if job_id is None:
        return
    with _lock:
        job = _jobs.get(job_id)
        if job is not None and job["finished"] is None:
            job.update(fields, updated=time.time())


def step(text, detail=None, mode="local"):
    _update(step=text, detail=detail, mode=mode)


def email(email_id, done=None, total=None):
    fields = {"email": email_id}
    if done is not None:
        fields["done"] = done
    if total is not None:
        fields["total"] = total
    _update(**fields)


def progress(done, total):
    _update(done=done, total=total)


def finish(job_id, error=None):
    with _lock:
        job = _jobs.get(job_id)
        if job is not None:
            now = time.time()
            job.update(finished=now, updated=now, error=error, mode="local",
                       step="Failed" if error else "Done", detail=error)


# ---------------------------------------------------------------- AI state
def ai_begin():
    with _lock:
        _ai["in_flight"] += 1
        _ai["calls"] += 1
        _ai["wait_until"] = None


def ai_end(error=None):
    with _lock:
        _ai["in_flight"] = max(0, _ai["in_flight"] - 1)
        if error:
            _ai["last_error"], _ai["last_error_at"] = error, time.time()


def ai_waiting(seconds):
    with _lock:
        _ai["wait_until"] = time.time() + seconds if seconds else None


# ---------------------------------------------------------------- snapshots
def _job_view(job, now):
    return {**job, "elapsed": round((job["finished"] or now) - job["started"], 1),
            "running": job["finished"] is None}


def job(job_id):
    now = time.time()
    with _lock:
        j = _jobs.get(job_id)
        return _job_view(j, now) if j else None


def snapshot():
    now = time.time()
    with _lock:
        for jid in [j for j, v in _jobs.items()
                    if v["finished"] and now - v["finished"] > KEEP_FINISHED_SECONDS]:
            _jobs.pop(jid, None)
        running = [_job_view(j, now) for j in _jobs.values() if j["finished"] is None]
        ai = dict(_ai)
    key = config.llm_api_key()
    waiting = max(0, round(ai["wait_until"] - now)) if ai["wait_until"] else 0
    recent_error = ai["last_error"] if ai["last_error_at"] and now - ai["last_error_at"] < 90 else None
    return {
        "ai": {
            "enabled": bool(key) and not config.ai_disabled(),
            "reason_off": None if key and not config.ai_disabled()
            else ("switched off" if config.ai_disabled() else "no API key configured"),
            "model": config.llm_model(),
            "in_flight": ai["in_flight"],
            "waiting_seconds": waiting,
            "calls": ai["calls"],
            "recent_error": recent_error,
        },
        "jobs": sorted(running, key=lambda j: j["started"]),
    }
