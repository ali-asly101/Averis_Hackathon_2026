"""
api.py — HTTP API over the pipeline for the web UI (and the cloud deploy).

Start it with:  python -m sdoc serve        (settings from .env: see config.py)

When frontend/dist exists (cd frontend && npm run build), the same server also
serves the UI at "/", so production is one process.

Endpoints:
  GET  /api/health                      settings (no secrets) + whether results exist
  GET  /api/summary                     dashboard numbers
  GET  /api/emails                      inbox rows
  GET  /api/emails/{id}                 one email: record, 7-field view, decision
  GET  /api/emails/{id}/attachments/{i} the i-th attachment's raw file (source evidence)
  GET  /api/review                      human review queue
  POST /api/review/{id}                 confirm / correct / override / reopen
  POST /api/run                         start a pipeline run (background)
  GET  /api/run                         run progress / last result
  POST /api/check                       Upload & Check: run one email + SI/BL files
  POST /api/batch                       Upload & Check: a whole inbox (zip / .json / .eml + files)
  GET  /api/batch/{id}                  batch progress + results
  GET  /api/batch/{id}/submission       the batch's submission.json (original email ids)
  GET  /api/sample-bundle               a small sample inbox zip to try the batch upload with
  GET  /api/activity[?job=ID]           live status: what's happening now, AI state, running jobs
  DELETE /api/emails/{id}               delete an uploaded email + its files + review decision
  DELETE /api/batch/{id}                delete a whole uploaded batch
  DELETE /api/uploads                   delete every upload (needs the admin token if one is set)
  (Emails of the provided dataset can't be deleted: submission.json must list all of them.)

Public-deploy guardrails (config.py): SDOC_AUTORUN, SDOC_RUN_COOLDOWN,
SDOC_ADMIN_TOKEN, SDOC_MAX_UPLOAD_MB, SDOC_CHECKS_PER_HOUR.
"""
import io
import json
import mimetypes
import os
import re
import secrets
import zipfile
import shutil
import threading
import time
import traceback
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from starlette.concurrency import run_in_threadpool

from . import activity
from . import batch as batch_mod
from . import config, ocr, pipeline, review, uploads
from .loader import Inbox

FIELDS = ["shipper", "consignee", "notify_party", "port_of_loading",
          "port_of_discharge", "container_count", "gross_weight_kg"]
FIELD_LABELS = {"shipper": "Shipper", "consignee": "Consignee", "notify_party": "Notify Party",
                "port_of_loading": "Port of Loading", "port_of_discharge": "Port of Discharge",
                "container_count": "Container Count", "gross_weight_kg": "Gross Weight (kg)"}

@asynccontextmanager
async def _lifespan(_app):
    # A fresh deploy has no results: process the inbox once, in the background,
    # so the site opens with a populated dashboard.
    out = config.out_dir()
    if not (out / "results.json").exists():
        seed = config.seed_dir()
        if seed and (seed / "results.json").is_file():
            out.mkdir(parents=True, exist_ok=True)
            shutil.copytree(seed, out, dirs_exist_ok=True)
            print(f"Loaded pre-computed results from {seed}")
        elif config.autorun():
            print("SDOC_AUTORUN: no results yet - processing the inbox in the background")
            _start_run(RunRequest())
    yield


app = FastAPI(title="SDOC shipping document verification API", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins(),
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

_lock = threading.RLock()        # one writer at a time: runs and review decisions
_uploads_lock = threading.Lock() # uploads.json only - never waits for a full run
_run = {"running": False, "done": 0, "total": 0, "started": None, "finished": None,
        "error": None, "summary": None, "mode": None}
_last_run_started = 0.0
_checks_by_ip = defaultdict(deque)       # ip -> timestamps of recent /api/check calls
_batches_by_ip = defaultdict(deque)      # ip -> timestamps of recent /api/batch calls
_batches = {}                            # batch_id -> progress (most recent first, capped)
_batch_running = threading.Event()


def _data():
    return config.data_source()


def _out():
    path = config.out_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _has_results():
    return (_out() / "results.json").exists()


def _inbox():
    """Dataset inbox + anything uploaded through Upload & Check."""
    return uploads.UploadInbox(Inbox(_data()))


def _rate_limited(bucket, ip, limit):
    """True if `ip` already used `limit` requests in the last hour (and records this one if not)."""
    recent, now = bucket[ip], time.time()
    while recent and now - recent[0] > 3600:
        recent.popleft()
    if len(recent) >= limit:
        return True
    recent.append(now)
    return False


def _client_ip(request):
    forwarded = request.headers.get("x-forwarded-for")   # behind a proxy (HF, Render...)
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# view helpers
# ---------------------------------------------------------------------------
def _basis(rec):
    """Short, honest 'how was this decided' label for the UI (instead of a
    made-up confidence %)."""
    report = rec.get("report") or {}
    docs = [d for d in (report.get("si_doc"), report.get("bl_doc")) if isinstance(d, dict)]
    ocr_docs = [d for d in docs if d.get("read_method") in ("ocr", "vision")]
    if ocr_docs:
        conf = min((d.get("ocr") or {}).get("confidence", 0) for d in ocr_docs)
        return {"kind": "ocr", "label": f"Scan read by OCR ({conf:.0%})" if conf else "Scan read by vision AI",
                "score": conf or None}
    if docs:
        sources = [s for d in docs for s in (d.get("field_sources") or {}).values()]
        if sources and all(s == "rules+ai" for s in sources):
            return {"kind": "cross_checked", "label": "Rules + AI agree", "score": 1.0}
        if any(s == "ai" for s in sources):
            return {"kind": "ai", "label": "AI-read", "score": None}
        if sources:
            return {"kind": "rules", "label": "Rule-based", "score": None}
    method = (rec.get("classification") or {}).get("method", "")
    if "->ai" in method:
        return {"kind": "ai", "label": "Classified by AI", "score": None}
    if rec.get("error"):
        return {"kind": "error", "label": "Processing error", "score": None}
    return {"kind": "rules", "label": "Rule-based", "score": None}


def _row(rec, final, decisions, pending):
    eid = rec["email_id"]
    entry = final.get(eid, rec["submission"])
    atts = rec.get("attachments") or []
    return {
        "id": eid,
        "subject": rec.get("subject") or "",
        "sender": rec.get("sender") or "",
        "attachments": len(atts),
        "attachment_types": sorted({a.rsplit(".", 1)[-1].lower() for a in atts if "." in a}),
        "category": entry["category"],
        "status": entry["status"],
        "review_reason": entry.get("review_reason"),
        "defect_fields": entry.get("defect_fields", []),
        "needs_review": eid in pending,
        "reviewed": eid in decisions,
        "retryable": bool(rec.get("retryable")),
        "basis": _basis(rec),
        "summary": rec.get("summary"),
        "source": rec.get("source", "dataset"),
        "batch_id": rec.get("batch_id"),
        "original_id": rec.get("original_id"),
    }


def _field_view(rec, decision):
    """The 7 fields, SI vs BL, ready to render. Uses (in order) the reviewer's
    corrected comparison, the automatic comparison, the unconfirmed proposal,
    or the raw partial reading."""
    report = rec.get("report") or {}
    proposal = report.get("proposed_result") or {}
    if decision and decision.get("field_report"):
        fr, source = decision["field_report"], "reviewer"
    elif report.get("field_report"):
        fr, source = report["field_report"], "comparison"
    elif proposal.get("field_report"):
        fr, source = proposal["field_report"], "proposed"
    else:
        si, bl = report.get("si_fields") or {}, report.get("bl_fields") or {}
        fr = {f: {"si": si.get(f), "bl": bl.get(f), "match": None} for f in FIELDS} if (si or bl) else {}
        source = "partial" if fr else None
    rows = []
    for f in FIELDS:
        e = fr.get(f) or {}
        rows.append({
            "field": f, "label": FIELD_LABELS[f],
            "si": e.get("si"), "bl": e.get("bl"), "match": e.get("match"),
            "method": e.get("method"), "similarity": e.get("similarity"),
            "si_source": e.get("si_source"), "bl_source": e.get("bl_source"),
            "si_evidence": e.get("si_evidence"), "bl_evidence": e.get("bl_evidence"),
            "readers_disagreed": e.get("si_readers_disagreed") or e.get("bl_readers_disagreed"),
        })
    return {"source": source, "fields": rows if fr else []}


def _documents(rec):
    report = rec.get("report") or {}
    out = []
    for side in ("si", "bl"):
        d = report.get(f"{side}_doc")
        if isinstance(d, dict):
            out.append({"side": side.upper(), "path": d.get("path"), "file_type": d.get("file_type"),
                        "read_method": d.get("read_method"), "document_type": d.get("document_type"),
                        "ocr": d.get("ocr"), "warnings": d.get("warnings", []),
                        "error": d.get("error"), "text_preview": d.get("text_preview", "")})
    return out


def _load():
    out = _out()
    records = review.load_records(out)
    decisions = review.load_decisions(out)
    final = review.final_submission(out)
    pending = {q["email_id"] for q in review.queue(out)}
    return records, decisions, final, pending


def _get_record(email_id):
    records, decisions, final, pending = _load()
    rec = records.get(email_id)
    if rec is None:
        raise HTTPException(404, f"Unknown email {email_id}")
    return rec, decisions, final, pending


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"ok": True, "has_results": _has_results(), "ocr_engine": ocr.available_engine(),
            "run_requires_token": bool(config.admin_token()),
            "max_upload_mb": config.max_upload_mb(),
            **{k: v for k, v in config.describe().items() if k not in ("env_file", "output")}}


def _upload_summary(uploaded, decisions, final, pending):
    """Numbers for emails uploaded through Upload & Check (kept apart from the
    official inbox so the dashboard's main numbers match submission.json)."""
    entries = [final.get(r["email_id"], r["submission"]) for r in uploaded]
    comparisons = [e for e in entries if e["category"] == "BL_COMPARISON"]
    cats: Dict[str, int] = {}
    for e in entries:
        cats[e["category"]] = cats.get(e["category"], 0) + 1
    ids = {r["email_id"] for r in uploaded}
    return {
        "emails": len(entries),
        "batches": len({r.get("batch_id") for r in uploaded if r.get("batch_id")}),
        "categories": cats,
        "comparisons": len(comparisons),
        "mismatches": sum(1 for e in comparisons if e["status"] == "MISMATCH"),
        "no_mismatch": sum(1 for e in comparisons if e["status"] == "OK"),
        "needs_review": len(ids & pending),
        "resolved_by_reviewer": len(ids & set(decisions)),
    }


@app.get("/api/summary")
def summary():
    """Main numbers = the provided inbox only (they match submission.json);
    "uploads" = what visitors added through Upload & Check."""
    if not _has_results():
        return {"has_results": False}
    records, decisions, final, pending = _load()
    dataset = [r for r in records.values() if r.get("source") != "upload"]
    uploaded = [r for r in records.values() if r.get("source") == "upload"]
    dataset_ids = {r["email_id"] for r in dataset}

    total = len(final)
    cats: Dict[str, int] = {}
    for e in final.values():
        cats[e["category"]] = cats.get(e["category"], 0) + 1
    comparisons = [e for e in final.values() if e["category"] == "BL_COMPARISON"]
    flagged_by_system = sum(1 for r in dataset if r.get("needs_review"))
    field_sources = [s for r in dataset for side in ("si_doc", "bl_doc")
                     for s in (((r.get("report") or {}).get(side) or {}).get("field_sources") or {}).values()]
    return {
        "has_results": True,
        "emails_processed": total,
        "categories": cats,
        "comparisons": len(comparisons),
        "mismatches": sum(1 for e in comparisons if e["status"] == "MISMATCH"),
        "no_mismatch": sum(1 for e in comparisons if e["status"] == "OK"),
        "needs_review": len(dataset_ids & pending),
        "resolved_by_reviewer": len(dataset_ids & set(decisions)),
        "automation_rate": round((total - flagged_by_system) / total, 4) if total else 0,
        # None = the AI was not used in this run (offline / rules-only), not "0% agreed"
        "cross_checked_rate": round(sum(s == "rules+ai" for s in field_sources) / len(field_sources), 4)
        if any(s in ("rules+ai", "ai") for s in field_sources) else None,
        "retryable_failures": sum(1 for r in dataset if r.get("retryable")),
        "last_run": _run.get("finished"),
        "uploads": _upload_summary(uploaded, decisions, final, pending),
    }


@app.get("/api/emails")
def list_emails():
    if not _has_results():
        return []
    records, decisions, final, pending = _load()
    rows = [_row(r, final, decisions, pending) for _, r in sorted(records.items())]
    uploaded = sorted((r for r in rows if r["source"] == "upload"), key=lambda r: r["id"], reverse=True)
    return uploaded + [r for r in rows if r["source"] != "upload"]   # newest uploads first


@app.get("/api/emails/{email_id}")
def get_email(email_id: str):
    rec, decisions, final, pending = _get_record(email_id)
    decision = decisions.get(email_id)
    report = rec.get("report") or {}
    return {
        **_row(rec, final, decisions, pending),
        "classification": rec.get("classification"),
        "body_preview": rec.get("body_preview"),
        "system_result": rec["submission"],
        "note": report.get("note"),
        "proposed_result": {k: v for k, v in (report.get("proposed_result") or {}).items()
                            if k != "field_report"} or None,
        "view": _field_view(rec, decision),
        "documents": _documents(rec),
        "decision": decision,
        "error": rec.get("error"),
    }


@app.get("/api/emails/{email_id}/attachments/{index}")
def get_attachment(email_id: str, index: int):
    """Serves only attachments listed on the email (no arbitrary paths)."""
    rec, *_ = _get_record(email_id)
    atts = rec.get("attachments") or []
    if not 0 <= index < len(atts):
        raise HTTPException(404, "No such attachment")
    path = atts[index]
    try:
        data = _inbox().read_bytes(path)
    except Exception as e:
        raise HTTPException(404, f"Attachment unavailable: {type(e).__name__}")
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return Response(data, media_type=ctype, headers={"Content-Disposition": f'inline; filename="{name}"'})


@app.get("/api/review")
def review_queue():
    if not _has_results():
        return []
    return review.queue(_out())


class Decision(BaseModel):
    action: Literal["confirm", "correct", "override", "reopen"]
    reviewer: Optional[str] = None
    note: Optional[str] = None
    si_fields: Optional[Dict[str, Any]] = None
    bl_fields: Optional[Dict[str, Any]] = None
    category: Optional[str] = None
    status: Optional[str] = None
    defect_fields: Optional[List[str]] = None
    review_reason: Optional[str] = None


@app.post("/api/review/{email_id}")
def decide(email_id: str, body: Decision):
    if _run["running"]:
        raise HTTPException(409, "A pipeline run is in progress; try again when it finishes")
    with _lock:
        try:
            review.decide(_out(), email_id, body.action, reviewer=body.reviewer, note=body.note,
                          si_fields=body.si_fields, bl_fields=body.bl_fields,
                          category=body.category, status=body.status,
                          defect_fields=body.defect_fields, review_reason=body.review_reason)
        except review.ReviewError as e:
            raise HTTPException(400, str(e))
    return get_email(email_id)


class RunRequest(BaseModel):
    retry: bool = False
    no_ai: bool = False
    limit: Optional[int] = None


def _do_run(req: RunRequest):
    previous = os.environ.get("SDOC_DISABLE_AI")
    try:
        if req.no_ai:
            os.environ["SDOC_DISABLE_AI"] = "1"
        activity.start("run", "run", label="Processing the provided inbox")
        token = activity.bind("run")
        try:
            with _lock:
                submission, records = pipeline.run(
                    _data(), _out(), req.limit, retry=req.retry,
                    progress=lambda done, total: _run.update(done=done, total=total))
        finally:
            activity.unbind(token)
        _run["summary"] = {"emails": len(submission),
                           "retryable_failures": sum(1 for r in records if r.get("retryable"))}
    except Exception as e:
        _run["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    finally:
        activity.finish("run", _run.get("error"))
        if req.no_ai:
            if previous is None:
                os.environ.pop("SDOC_DISABLE_AI", None)
            else:
                os.environ["SDOC_DISABLE_AI"] = previous
        _run.update(running=False, finished=time.strftime("%Y-%m-%dT%H:%M:%S"))


def _start_run(req: RunRequest):
    global _last_run_started
    _last_run_started = time.time()
    _run.update(running=True, done=0, total=0, error=None, summary=None,
                started=time.strftime("%Y-%m-%dT%H:%M:%S"), finished=None,
                mode="retry" if req.retry else ("rules only" if req.no_ai else "full"))
    threading.Thread(target=_do_run, args=(req,), daemon=True).start()


@app.post("/api/run")
def start_run(req: RunRequest, x_admin_token: Optional[str] = Header(default=None)):
    token = config.admin_token()
    if token and x_admin_token != token:
        raise HTTPException(401, "Admin token required to start a full run")
    if _run["running"]:
        raise HTTPException(409, "A run is already in progress")
    wait = config.run_cooldown_seconds() - (time.time() - _last_run_started)
    if wait > 0 and not (token and x_admin_token == token):
        raise HTTPException(429, f"A run was started recently - try again in {int(wait) + 1}s")
    _start_run(req)
    return _run


@app.post("/api/check")
async def check_upload(
    request: Request,
    subject: str = Form(""),
    sender: str = Form(""),
    body: str = Form(""),
    job_id: str = Form(""),
    si: Optional[UploadFile] = File(None),
    bl: Optional[UploadFile] = File(None),
):
    """Upload & Check: classify an email and, if SI/BL are attached, read and
    compare them. The result joins the inbox and (if needed) the review queue."""
    ip = _client_ip(request)
    recent = _checks_by_ip[ip]
    now = time.time()
    while recent and now - recent[0] > 3600:
        recent.popleft()
    if len(recent) >= config.checks_per_hour():
        raise HTTPException(429, "Upload limit reached for this hour - please try again later")

    limit = config.max_upload_mb() * 1024 * 1024
    files = {}
    for kind, up in (("si", si), ("bl", bl)):
        if up is not None and up.filename:
            data = await up.read(limit + 1)
            files[kind] = (up.filename, data)
    try:
        email = uploads.save_upload(subject, sender, body, si=files.get("si"), bl=files.get("bl"))
    except uploads.UploadError as e:
        raise HTTPException(400, str(e))
    recent.append(now)

    # The browser picks the job id so it can poll /api/activity?job=... while
    # this request is still running. The work runs in a worker thread: OCR or
    # AI can take a while and must not block every other request.
    job = job_id if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id or "") else f"check_{secrets.token_hex(4)}"
    activity.start(job, "check", total=1, label=email["subject"], target=email["email_id"])

    def work():
        token = activity.bind(job)
        try:
            record = pipeline.process_one(email, _inbox())
            with _uploads_lock:
                uploads.append_record(record)
            activity.progress(1, 1)
        finally:
            activity.unbind(token)

    try:
        await run_in_threadpool(work)
        activity.finish(job)
    except Exception as e:
        activity.finish(job, f"{type(e).__name__}: {e}")
        raise
    return get_email(email["email_id"])


@app.get("/api/run")
def run_status():
    return {**_run, "activity": activity.job("run")}


def _slow_email(email):
    """Attachments other than plain text mean OCR and/or AI calls (and possibly
    rate-limit waits): seconds to minutes for this one email."""
    return any(not str(a).lower().endswith(".txt") for a in email.get("attachments") or [])


def _do_batch(batch_id, emails):
    state = _batches[batch_id]
    inbox, chunk = _inbox(), []
    last_flush = time.time()
    token = activity.bind(batch_id)

    def flush():
        nonlocal chunk, last_flush
        if chunk:
            with _uploads_lock:
                uploads.append_records(chunk)
            chunk, last_flush = [], time.time()

    try:
        for i, email in enumerate(emails, 1):
            activity.email(email.get("original_id"), done=i - 1, total=len(emails))
            if _slow_email(email):
                flush()        # save finished work before a slow email, so a restart can't lose it
            record = pipeline.process_one(email, inbox)
            record.update(batch_id=batch_id, original_id=email["original_id"])
            chunk.append(record)
            state["ids"].append(record["email_id"])
            # and save often anyway: a crash or restart then loses at most a few emails' work
            if len(chunk) >= 5 or time.time() - last_flush > 2 or i == len(emails):
                flush()
            state["done"] = i
    except Exception as e:
        state["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    finally:
        activity.unbind(token)
        activity.finish(batch_id, state["error"])
        state.update(running=False, finished=time.strftime("%Y-%m-%dT%H:%M:%S"))
        uploads.write_batch_meta(batch_id, finished=state["finished"], error=state["error"])
        _batch_running.clear()


def _batch_records(batch_id, state=None):
    state = state if state is not None else _batches.get(batch_id)
    if state is not None:                       # this server run: exact ids, in order
        wanted = set(state["ids"])
        found = {r["email_id"]: r for r in uploads.load_records() if r.get("email_id") in wanted}
        return [found[i] for i in state["ids"] if i in found]
    return [r for r in uploads.load_records() if r.get("batch_id") == batch_id]


@app.post("/api/batch")
async def start_batch(request: Request, files: List[UploadFile] = File(...)):
    """Upload a whole inbox: a zip in the dataset layout, email .json / .eml
    files, plus attachment files. Processed in the background."""
    if _batch_running.is_set():
        raise HTTPException(409, "Another batch is being processed - try again in a moment")
    limit = batch_mod.max_batch_bytes()
    received, total = [], 0
    for up in files:
        data = await up.read(limit + 1)
        total += len(data)
        if total > limit:
            raise HTTPException(400, f"Upload is over the {config.max_batch_mb()} MB limit")
        received.append((up.filename or "file", data))
    try:
        parsed = await run_in_threadpool(batch_mod.parse_uploads, received)
    except uploads.UploadError as e:
        raise HTTPException(400, str(e))
    if _rate_limited(_batches_by_ip, _client_ip(request), config.batches_per_hour()):
        raise HTTPException(429, "Batch upload limit reached for this hour - please try again later")

    batch_id, emails = uploads.save_batch(parsed)
    uploads.write_batch_meta(batch_id, total=len(emails), started=time.strftime("%Y-%m-%dT%H:%M:%S"),
                             finished=None)
    _batch_running.set()
    _batches[batch_id] = {"id": batch_id, "running": True, "done": 0, "total": len(emails), "ids": [],
                          "attachments": len(parsed.attachments), "warnings": parsed.warnings[:50],
                          "error": None, "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "finished": None}
    for old in list(_batches)[:-20]:          # keep the 20 most recent
        _batches.pop(old, None)
    # Reply with a snapshot taken BEFORE processing starts: a tiny batch can
    # finish before this response is sent, and the client must still see
    # "running" so it polls /api/batch/{id} and receives the rows.
    reply = {k: v for k, v in _batches[batch_id].items() if k != "ids"}
    activity.start(batch_id, "batch", total=len(emails), label=f"{len(emails)} uploaded emails")
    threading.Thread(target=_do_batch, args=(batch_id, emails), daemon=True).start()
    return reply


@app.get("/api/batches")
def list_batches():
    """Recent uploaded batches, newest first: running ones (this server run) plus
    everything still stored. Lets the UI find results again after the page was
    left, reloaded, or opened in a new tab."""
    stored = {}
    for r in uploads.load_records():
        bid = r.get("batch_id")
        if not bid:
            continue
        b = stored.setdefault(bid, {"emails": 0, "mismatches": 0, "needs_review": 0, "ok": 0})
        b["emails"] += 1
        status = r["submission"]["status"]
        if r["submission"]["category"] == "BL_COMPARISON":
            b["mismatches" if status == "MISMATCH" else "needs_review" if status == "NEEDS_REVIEW" else "ok"] += 1
    on_disk = set()
    if config.uploads_dir().is_dir():
        on_disk = {p.parent.name for p in config.uploads_dir().glob("batch_*/_batch.json")}
    out = []
    for bid in set(stored) | set(_batches) | on_disk:
        live = _batches.get(bid) or {}
        meta = uploads.read_batch_meta(bid) or {}
        counts = stored.get(bid, {"emails": 0, "mismatches": 0, "needs_review": 0, "ok": 0})
        total = live.get("total", meta.get("total", counts["emails"]))
        interrupted = not live and not meta.get("finished") and counts["emails"] < total
        out.append({
            "id": bid,
            "running": bool(live.get("running")),
            "done": live.get("done", counts["emails"]),
            "total": total,
            "started": live.get("started") or meta.get("started"),
            "finished": live.get("finished") or meta.get("finished"),
            "error": live.get("error") or ("Interrupted by a server restart" if interrupted else meta.get("error")),
            **counts,
        })
    return sorted(out, key=lambda b: b["id"], reverse=True)


@app.get("/api/batch/{batch_id}")
def batch_status(batch_id: str, rows: bool = True):
    """rows=false: progress only (cheap enough to poll every second)."""
    # Snapshot the progress BEFORE reading results. The worker stores results
    # before marking itself finished, so a snapshot that says "finished" always
    # comes with complete results (reading live progress afterwards could pair
    # "finished" with results read a moment too early).
    live = _batches.get(batch_id)
    state = {**live, "ids": list(live["ids"])} if live is not None else None
    records = _batch_records(batch_id, state)
    meta = uploads.read_batch_meta(batch_id) if state is None else None
    if state is None and not records and not meta:
        raise HTTPException(404, "Unknown batch")
    if state is None:                     # not running in this server process
        meta = meta or {}
        total = meta.get("total", len(records))
        interrupted = not meta.get("finished") and len(records) < total
        state = {"id": batch_id, "running": False, "done": len(records), "total": total,
                 "warnings": [], "finished": meta.get("finished"), "started": meta.get("started"),
                 "error": (f"Interrupted: the server restarted while this batch was running "
                           f"({len(records)} of {total} emails were checked). Upload it again for the rest.")
                 if interrupted else meta.get("error")}
    base = {**{k: v for k, v in state.items() if k != "ids"}, "activity": activity.job(batch_id)}
    if not rows:
        return base
    records_all, decisions, final, pending = _load()
    row_list = [{**_row(r, final, decisions, pending), "original_id": r.get("original_id")} for r in records]
    counts = {}
    for row in row_list:
        key = row["category"] if row["category"] != "BL_COMPARISON" else f"BL_COMPARISON/{row['status']}"
        counts[key] = counts.get(key, 0) + 1
    return {**base, "counts": counts, "rows": row_list}


@app.get("/api/batch/{batch_id}/submission")
def batch_submission(batch_id: str):
    """submission.json for the batch, keyed by the uploader's own email ids -
    the same shape as sample_submission.json, ready to score."""
    records = _batch_records(batch_id)
    if not records:
        raise HTTPException(404, "Unknown or unfinished batch")
    submission = {r.get("original_id") or r["email_id"]: r["submission"] for r in records}
    return Response(json.dumps(submission, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="submission_{batch_id}.json"'})


@app.get("/api/activity")
def get_activity(job: Optional[str] = None):
    """What's happening right now: running jobs with their current step, and the
    AI's state (on/off, calls in flight, rate-limit countdown, recent errors)."""
    snap = activity.snapshot()
    if job:
        snap["job"] = activity.job(job)
    return snap


def _running_batch_ids():
    return {bid for bid, st in _batches.items() if st.get("running")}


@app.delete("/api/emails/{email_id}")
def delete_email(email_id: str):
    rec, *_ = _get_record(email_id)
    if rec.get("source") != "upload":
        raise HTTPException(403, "This email is part of the provided dataset and can't be deleted "
                                 "(the submission must include every dataset email). "
                                 "You can reopen its review decision instead.")
    if rec.get("batch_id") in _running_batch_ids():
        raise HTTPException(409, "Its batch is still being processed - try again when it finishes")
    with _uploads_lock:
        result = uploads.delete_email(email_id)
    if result is None:
        raise HTTPException(404, "Already deleted")
    return {"deleted": result}


@app.delete("/api/batch/{batch_id}")
def delete_batch(batch_id: str):
    if batch_id in _running_batch_ids():
        raise HTTPException(409, "This batch is still being processed - try again when it finishes")
    with _uploads_lock:
        result = uploads.delete_batch(batch_id)
    _batches.pop(batch_id, None)
    if result is None:
        raise HTTPException(404, "Unknown or already deleted batch")
    return {"deleted": result}


@app.delete("/api/uploads")
def delete_all_uploads(x_admin_token: Optional[str] = Header(default=None)):
    token = config.admin_token()
    if token and x_admin_token != token:
        raise HTTPException(401, "Admin token required to delete every upload")
    if _running_batch_ids():
        raise HTTPException(409, "A batch is still being processed - try again when it finishes")
    with _uploads_lock:
        result = uploads.delete_all()
    _batches.clear()
    return {"deleted": result}


_SAMPLE_IDS = ["email_001", "email_002", "email_003", "email_004", "email_005", "email_013",
               "email_015", "email_017", "email_021", "email_025", "email_055", "email_059",
               "email_208", "email_501", "email_506", "email_507", "email_511", "email_512",
               "email_516", "email_517"]


@app.get("/api/sample-bundle")
def sample_bundle():
    """A small inbox (20 emails, mixed categories, formats and edge cases) in the
    dataset layout, to try the batch upload. Never includes the answer key."""
    inbox = Inbox(_data())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        added = 0
        for eid in _SAMPLE_IDS:
            try:
                rec = inbox.get(eid)
            except Exception:
                continue
            zf.writestr(f"sample_inbox/inbox/{eid}.json", json.dumps(rec, indent=2))
            for att in rec.get("attachments") or []:
                try:
                    zf.writestr(f"sample_inbox/{att}", inbox.read_bytes(att))
                except Exception:
                    pass
            added += 1
    if not added:
        raise HTTPException(404, "No sample emails available on this server")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="sample_inbox.zip"'})


# ---------------------------------------------------------------------------
# built frontend (production): frontend/dist served at "/"
# ---------------------------------------------------------------------------
_DIST = config.ROOT / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        target = _DIST / path
        if path and target.is_file() and _DIST in target.resolve().parents:
            return FileResponse(target)
        return FileResponse(_DIST / "index.html")
