"""
review.py — human review: a person confirms or corrects a case, and the
report is updated. Plain functions used by the web API and the CLI
(python -m sdoc review ...).

Files in the output folder (written by pipeline.py):
  results.json            full per-email records (system output)
  submission.json         the system's own answer (what the self-eval scores)
  review_decisions.json   every human decision, with who/when/why (audit trail)
  final_submission.json   submission.json with human decisions applied
  report.md               readable discrepancy report, incl. review status

Actions:
  confirm   accept the system's proposed result (e.g. an OCR-read scan)
  correct   fix one or more SI/BL values; the comparison is re-run on them
  override  set category / status / defect fields / review reason directly
  reopen    undo the decision, back into the queue
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import compare

CATEGORIES = ["BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]
STATUSES = ["OK", "MISMATCH", "NEEDS_REVIEW"]
REVIEW_REASONS = [None, "wrong_doc_type", "missing_attachment", "unreadable", "missing_value"]


class ReviewError(ValueError):
    pass


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
def _read(out_dir, name, default):
    path = Path(out_dir) / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _write(out_dir, name, data):
    path = Path(out_dir) / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def load_records(out_dir):
    """Dataset results + emails uploaded through the web UI (uploads.json).
    Uploads are reviewable but never part of submission.json."""
    records = {r["email_id"]: r for r in _read(out_dir, "results.json", []) if r.get("email_id")}
    for r in _read(out_dir, "uploads.json", []):
        if r.get("email_id"):
            records[r["email_id"]] = r
    return records


def load_decisions(out_dir):
    return _read(out_dir, "review_decisions.json", {})


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------
def queue(out_dir, include_decided=False):
    """Cases waiting for a human, most useful first (ones with a proposal)."""
    records, decisions = load_records(out_dir), load_decisions(out_dir)
    items = []
    for eid, rec in records.items():
        if not rec.get("needs_review"):
            continue
        decided = eid in decisions
        if decided and not include_decided:
            continue
        report = rec.get("report") or {}
        proposal = report.get("proposed_result") or {}
        items.append({
            "email_id": eid,
            "subject": rec.get("subject"),
            "category": rec["submission"]["category"],
            "status": rec["submission"]["status"],
            "review_reason": rec["submission"].get("review_reason"),
            "why": report.get("note") or (rec.get("classification") or {}).get("review_reason"),
            "proposed": proposal.get("status"),
            "retryable": rec.get("retryable", False),
            "decided": decided,
            "original_id": rec.get("original_id"),
            "source": rec.get("source", "dataset"),
        })
    # the provided inbox first, then uploads; cases with a ready proposal first
    items.sort(key=lambda i: (i["source"] == "upload", i["proposed"] is None, i["email_id"]))
    return items


def get_case(out_dir, email_id):
    rec = load_records(out_dir).get(email_id)
    if rec is None:
        raise ReviewError(f"Unknown email_id {email_id!r}")
    return {"record": rec, "decision": load_decisions(out_dir).get(email_id)}


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------
def _entry(category, status, review_reason=None, defect_fields=()):
    defect_fields = sorted(set(defect_fields or []))
    return {"category": category, "status": status,
            "review_reason": review_reason if status == "NEEDS_REVIEW" else None,
            "defect_fields": defect_fields if status == "MISMATCH" else [],
            "has_defect": status == "MISMATCH"}


def _current_fields(rec):
    report = rec.get("report") or {}
    si = dict(report.get("si_fields") or {})
    bl = dict(report.get("bl_fields") or {})
    for field, fr in (report.get("field_report") or {}).items():   # clean OK/MISMATCH cases
        si.setdefault(field, fr.get("si"))
        bl.setdefault(field, fr.get("bl"))
    return si, bl


def decide(out_dir, email_id, action, reviewer=None, note=None,
           si_fields=None, bl_fields=None, category=None, status=None,
           defect_fields=None, review_reason=None):
    """Apply one human decision, save it, and refresh the final outputs.
    Returns the saved decision."""
    records, decisions = load_records(out_dir), load_decisions(out_dir)
    rec = records.get(email_id)
    if rec is None:
        raise ReviewError(f"Unknown email_id {email_id!r}")
    system = rec["submission"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if action == "reopen":
        decisions.pop(email_id, None)
        _write(out_dir, "review_decisions.json", decisions)
        refresh_outputs(out_dir)
        return None

    if action == "confirm":
        proposal = (rec.get("report") or {}).get("proposed_result") or {}
        if proposal.get("status") not in ("OK", "MISMATCH"):
            raise ReviewError("Nothing to confirm: this case has no complete proposed result. "
                              "Use 'correct' to supply the values, or 'override'.")
        final = _entry("BL_COMPARISON", proposal["status"], None, proposal["defect_fields"])
        detail = {"confirmed_proposal": proposal["status"]}

    elif action == "correct":
        si, bl = _current_fields(rec)
        si.update(si_fields or {})
        bl.update(bl_fields or {})
        defects, field_report, implausible = compare.compare_fields(si, bl)
        blank = [f for f in defects if field_report[f]["method"] == "missing_value"]
        if blank or implausible:
            raise ReviewError(f"Still missing/implausible after correction: {blank or implausible}")
        final = _entry("BL_COMPARISON", "MISMATCH" if defects else "OK", None, defects)
        detail = {"si_fields": si, "bl_fields": bl, "field_report": field_report,
                  "corrected": {"si": si_fields or {}, "bl": bl_fields or {}}}

    elif action == "override":
        cat = category or system["category"]
        st = status or ("OK" if cat != "BL_COMPARISON" else system["status"])
        if cat not in CATEGORIES or st not in STATUSES:
            raise ReviewError(f"Bad category/status: {cat}/{st}")
        if cat != "BL_COMPARISON" and st != "OK":
            raise ReviewError("Non-comparison emails always have status OK")
        rr = review_reason if review_reason is not None else system.get("review_reason")
        if st == "NEEDS_REVIEW" and rr not in REVIEW_REASONS[1:]:
            raise ReviewError(f"NEEDS_REVIEW needs a review_reason from {REVIEW_REASONS[1:]}")
        final = _entry(cat, st, rr, defect_fields or [])
        detail = {}
    else:
        raise ReviewError(f"Unknown action {action!r}")

    decision = {"action": action, "reviewer": reviewer, "note": note, "at": now,
                "system_result": system, "final": final, **detail}
    history = (decisions.get(email_id) or {}).get("history", [])
    if email_id in decisions:
        history = history + [{k: v for k, v in decisions[email_id].items() if k != "history"}]
    decision["history"] = history
    decisions[email_id] = decision
    _write(out_dir, "review_decisions.json", decisions)
    refresh_outputs(out_dir)
    return decision


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------
def final_submission(out_dir):
    submission = _read(out_dir, "submission.json", {})
    for eid, d in load_decisions(out_dir).items():
        if eid in submission:
            submission[eid] = d["final"]
    return submission


def refresh_outputs(out_dir):
    _write(out_dir, "final_submission.json", final_submission(out_dir))
    (Path(out_dir) / "report.md").write_text(render_report(out_dir), encoding="utf-8")


def render_report(out_dir):
    records, decisions = load_records(out_dir), load_decisions(out_dir)
    final = final_submission(out_dir)
    lines = ["# Shipping document verification report", ""]
    comp = [eid for eid, e in final.items() if e["category"] == "BL_COMPARISON"]
    pending = [q["email_id"] for q in queue(out_dir)]
    lines += [f"- Emails processed: {len(final)}",
              f"- Document-comparison requests: {len(comp)}",
              f"- Mismatches: {sum(1 for e in comp if final[e]['status'] == 'MISMATCH')}",
              f"- Waiting for human review: {len(pending)}",
              f"- Resolved by a reviewer: {len(decisions)}", ""]

    def fields_table(field_report, only):
        rows = ["| Field | SI | BL |", "|---|---|---|"]
        for f in only:
            fr = field_report.get(f, {})
            rows.append(f"| {f} | {fr.get('si')} | {fr.get('bl')} |")
        return rows

    for eid in sorted(comp):
        e, rec = final[eid], records.get(eid, {})
        report, d = rec.get("report") or {}, decisions.get(eid)
        lines.append(f"## {eid} — {rec.get('subject') or ''}")
        tag = f" (reviewed by {d.get('reviewer') or 'a person'}: {d['action']})" if d else ""
        if e["status"] == "OK":
            lines.append(f"**No mismatch detected.**{tag}")
        elif e["status"] == "MISMATCH":
            fr = (d or {}).get("field_report") or report.get("field_report") \
                or (report.get("proposed_result") or {}).get("field_report") or {}
            lines.append(f"**Mismatch in {', '.join(e['defect_fields'])}**{tag}")
            lines += [""] + fields_table(fr, e["defect_fields"])
        else:
            lines.append(f"**Needs human review — {e['review_reason']}**{tag}")
            if report.get("note"):
                lines.append(f"Reason: {report['note']}")
            prop = report.get("proposed_result") or {}
            if prop.get("status") == "MISMATCH":
                lines.append("Proposed (unconfirmed): mismatch in " + ", ".join(prop["defect_fields"]))
                lines += [""] + fields_table(prop["field_report"], prop["defect_fields"])
            elif prop.get("status") == "OK":
                lines.append("Proposed (unconfirmed): no mismatch detected.")
        lines.append("")
    return "\n".join(lines)
