"""
pipeline.py — runs the whole thing: classify -> read documents -> extract -> compare.

Usually run through the CLI:  python -m sdoc run [--no-ai] [--retry] [--submit]

Outputs (in SDOC_OUT, default ./output):
  submission.json        the system's answer, exactly the sample_submission shape
  review_queue.json      every case a human should look at, with reason + evidence
  results.json           full per-email records (for the frontend / debugging)
  final_submission.json  submission + human decisions (see review.py)
  report.md              readable discrepancy report

For the web API: process_one(email, inbox) handles one email and returns the
same record that goes into results.json; run(...) does a whole inbox.
Settings: see config.py / .env.example.
"""
import json
import time
from collections import Counter
from pathlib import Path

from . import activity, classify, compare, config, review
from .email_utils import clean_email_body
from .extraction import process_email
from .loader import Inbox

NON_COMPARISON_ENTRY = {"status": "OK", "review_reason": None,
                        "defect_fields": [], "has_defect": False}
_RETRYABLE_CLASSIFY = {"ai_rate_limited", "ai_call_failed", "ai_unexpected_response", "ai_unavailable"}


def process_one(email, inbox):
    """Classify one email and, if it's a comparison request, read + compare.
    Never raises. Returns a JSON-serialisable dict."""
    email_id = email.get("email_id") if isinstance(email, dict) else None
    is_dict = isinstance(email, dict)
    atts = email.get("attachments") if is_dict else None
    record = {"email_id": email_id,
              "subject": email.get("subject") if is_dict else None,
              "sender": email.get("from") if is_dict else None,
              "attachments": [a for a in atts if isinstance(a, str)] if isinstance(atts, list) else [],
              # what the sender wrote (no banners / forwarded thread), for the detail page
              "body_preview": clean_email_body(email.get("body"))[:1500] if is_dict else "",
              "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        activity.email(email.get("original_id") or email_id)   # the uploader's own id
        activity.step("Classifying the email", detail=record["subject"], mode="local")
        meta = classify.classify_email_with_meta(email)
        record["classification"] = meta
        category = meta["category"]
        retryable = meta.get("review_reason") in _RETRYABLE_CLASSIFY

        if category == "BL_COMPARISON":
            activity.step("Document-comparison request - reading the attachments", mode="local")
            extraction = process_email(email, inbox)
            activity.step("Comparing the 7 fields (SI vs BL)", mode="compare")
            report = compare.build_report(extraction)
            record["report"] = report
            record["summary"] = compare.render_summary(report)
            record["submission"] = compare.build_submission_entry(report)
            retryable = retryable or bool(extraction.get("retryable"))
        else:
            record["submission"] = {"category": category, **NON_COMPARISON_ENTRY}
            record["summary"] = f"{email_id}: {category}"

        record["retryable"] = retryable
        record["needs_review"] = (meta["needs_review"]
                                  or record["submission"]["status"] == "NEEDS_REVIEW")
    except Exception as e:  # one bad email must never stop the batch
        record["error"] = f"{type(e).__name__}: {e}"
        record["submission"] = {"category": "GENERAL", **NON_COMPARISON_ENTRY}
        record["summary"] = f"{email_id}: pipeline error ({type(e).__name__}) - retry"
        record["needs_review"] = True
        record["retryable"] = True
    return record


def _review_entry(rec):
    sub = rec["submission"]
    report = rec.get("report") or {}
    cls = rec.get("classification") or {}
    reasons = []
    if cls.get("needs_review"):
        reasons.append(f"classification: {cls.get('review_reason')}")
    if sub["status"] == "NEEDS_REVIEW":
        reasons.append(f"comparison: {sub['review_reason']}")
    if rec.get("error"):
        reasons.append(f"processing error: {rec['error']}")
    return {
        "email_id": rec["email_id"],
        "subject": rec.get("subject"),
        "category": sub["category"],
        "status": sub["status"],
        "reasons": reasons,
        "detail": report.get("note"),
        "retryable": rec.get("retryable", False),
        "si_file": report.get("si_file"),
        "bl_file": report.get("bl_file"),
        "proposed_result": report.get("proposed_result"),
        "field_report": report.get("field_report") or None,
    }


def validate_submission(submission, sample):
    """Every sample email_id present, same keys per entry, valid values."""
    problems = []
    missing = set(sample) - set(submission)
    extra = set(submission) - set(sample)
    if missing:
        problems.append(f"{len(missing)} email_ids missing, e.g. {sorted(missing)[:3]}")
    if extra:
        problems.append(f"{len(extra)} unexpected email_ids, e.g. {sorted(extra)[:3]}")
    for eid, entry in submission.items():
        if eid in sample and set(entry) != set(sample[eid]):
            problems.append(f"{eid}: keys {sorted(entry)} != {sorted(sample[eid])}")
            break
        if entry["category"] not in classify.CATEGORIES:
            problems.append(f"{eid}: bad category {entry['category']!r}")
            break
    return problems


def _write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def print_banner():
    info = config.describe()
    print("SDOC pipeline")
    for key in ("data", "output", "ai", "llm_model", "extraction_mode", "ocr_policy", "env_file"):
        print(f"  {key:16s} {info[key]}")


def run(source=None, out_dir=None, limit=None, retry=False, progress=None):
    """source/out_dir default to SDOC_DATA / SDOC_OUT.
    progress: optional callable(done, total) - used by the API for a live progress bar."""
    source = source or config.data_source()
    inbox = Inbox(source)
    out = Path(out_dir) if out_dir else config.out_dir()
    out.mkdir(parents=True, exist_ok=True)

    previous = {}
    if retry:
        try:
            previous = {r["email_id"]: r for r in json.loads((out / "results.json").read_text(encoding="utf-8"))}
        except FileNotFoundError:
            print("No previous results.json - doing a full run instead")

    emails = inbox.emails()
    if limit:
        emails = emails[:limit]
    if previous:
        todo = {eid for eid, r in previous.items() if r.get("retryable")}
        print(f"Retrying {len(todo)} email(s) that failed retryably")
    else:
        todo = None

    records, started = [], time.time()
    for i, email in enumerate(emails, 1):
        eid = email.get("email_id")
        if todo is not None and eid not in todo and eid in previous:
            records.append(previous[eid])
        else:
            records.append(process_one(email, inbox))
        activity.progress(i, len(emails))
        if progress:
            progress(i, len(emails))
        if i % 50 == 0 or i == len(emails):
            print(f"  ...{i}/{len(emails)} ({time.time() - started:.0f}s)")

    submission = {r["email_id"]: r["submission"] for r in records if r.get("email_id")}
    review_items = [_review_entry(r) for r in records if r.get("needs_review")]
    _write_json(out / "submission.json", submission)
    _write_json(out / "review_queue.json", review_items)
    _write_json(out / "results.json", records)
    review.refresh_outputs(out)           # final_submission.json + report.md

    try:
        sample = inbox.sample_submission()
        if limit:
            sample = {k: v for k, v in sample.items() if k in submission}
        problems = validate_submission(submission, sample)
        print("submission shape: OK" if not problems else "submission shape PROBLEMS:\n  "
              + "\n  ".join(problems))
    except Exception as e:
        print(f"(could not load sample_submission.json to validate: {e})")

    cats = Counter(s["category"] for s in submission.values())
    stats = Counter(s["status"] for s in submission.values())
    failed = [r["email_id"] for r in records if r.get("retryable")]
    print(f"{len(submission)} emails | {dict(cats)} | {dict(stats)} | review queue: {len(review_items)}")
    if failed:
        print(f"{len(failed)} email(s) hit a retryable failure (AI/OCR/network), e.g. {failed[:5]}"
              f" -> fix the cause, then: python -m sdoc run --retry")
    return submission, records
