"""
extraction.py — Stage 2: read the SI + BL attachments of a BL_COMPARISON
email into the 7 comparison fields each (via document_reader.py).

process_email(email, inbox) never raises. It returns one of:

  {"email_id", "status": "ok", "si_fields", "bl_fields", "si_doc", "bl_doc", ...}
  {"email_id", "status": "awaiting_documents", "reason"}   # "please send the draft BL"
  {"email_id", "status": "escalate", "reason", ...}        # can't compare -> human review

si_doc / bl_doc are the full document records (read method, OCR confidence,
where each value came from, evidence snippets, warnings): the source evidence
a reviewer needs.

Escalation reasons start with a fixed phrase that compare.py maps onto the
README's review_reason values:
  "Missing attachment(s)"          -> missing_attachment
  "Wrong document type"            -> wrong_doc_type
  "Unsupported attachment format"  -> wrong_doc_type
  "Could not read attachment"      -> unreadable
  "Field extraction failed"        -> missing_value

Scanned documents (SDOC_OCR_POLICY):
  "review" (default)  OCR/vision values are a PROPOSAL: the email goes to a
                      human as NEEDS_REVIEW / unreadable, with the proposed
                      values and comparison attached, ready to confirm.
                      (OCR misreads are real: "6 x 40'HC" read as "8 x 40}".)
  "trust"             if OCR confidence >= SDOC_OCR_TRUST_MIN (default 0.95)
                      and every field was read, compare automatically.
"""
from . import config
from .document_reader import is_wrong_type, read_document
from .email_utils import find_si_bl_attachments, is_send_bl_request


def _escalate(email_id, reason, retryable=False, **extra):
    return {"email_id": email_id, "status": "escalate", "reason": reason,
            "retryable": retryable, **extra}


def _slim(doc):
    """Document record without the bulky preview, for places that repeat it."""
    return {k: v for k, v in doc.items() if k != "text_preview"}


def process_email(email, inbox):
    if not isinstance(email, dict):
        return _escalate(None, f"Could not read attachment: malformed email record ({type(email).__name__})")
    email_id = email.get("email_id")

    raw_atts = email.get("attachments")
    attachments = [a for a in raw_atts if isinstance(a, str) and a.strip()] \
        if isinstance(raw_atts, list) else []

    # --- nothing attached ----------------------------------------------------
    if not attachments:
        if is_send_bl_request(email):
            return {"email_id": email_id, "status": "awaiting_documents",
                    "reason": "Sender asked for the draft BL to be sent; nothing to compare yet"}
        return _escalate(email_id, "Missing attachment(s): SI, BL")

    # --- find the SI and BL ----------------------------------------------------
    si_files, bl_files = find_si_bl_attachments(attachments)
    missing = [name for name, files in (("SI", si_files), ("BL", bl_files)) if not files]
    if missing:
        return _escalate(email_id, f"Missing attachment(s): {', '.join(missing)}")

    docs = {"SI": read_document(si_files[0], inbox, "SI"),
            "BL": read_document(bl_files[0], inbox, "BL")}
    evidence = {"si_file": si_files[0], "bl_file": bl_files[0],
                "si_doc": docs["SI"], "bl_doc": docs["BL"]}
    retryable = any(d["retryable"] for d in docs.values())

    # --- could we read both? ------------------------------------------------------
    for kind, doc in docs.items():
        code = doc["error_code"]
        if code == "read_failed" and not doc["retryable"]:
            return _escalate(email_id, f"Missing attachment(s): {kind} (referenced but not provided)",
                             retryable=False, **evidence)
        if code == "unsupported_format":
            return _escalate(email_id, f"Unsupported attachment format ({kind}): {doc['file_type']}",
                             retryable=retryable, **evidence)
        if code:
            return _escalate(email_id, f"Could not read attachment ({kind}): {doc['error']}",
                             retryable=retryable, **evidence)

    # --- right kind of document? ---------------------------------------------------
    for kind, doc in docs.items():
        if is_wrong_type(doc["document_type"], kind):
            pretty = doc["document_type"].replace("_", " ").title()
            return _escalate(email_id, f"Wrong document type ({kind}): looks like a {pretty}",
                             retryable=retryable, **evidence)

    si_fields, bl_fields = docs["SI"]["fields"], docs["BL"]["fields"]
    fields = {"si_fields": si_fields, "bl_fields": bl_fields}

    # --- scanned documents -------------------------------------------------------------
    scanned = [k for k, d in docs.items() if d["read_method"] in ("ocr", "vision")]
    if scanned:
        complete = all(v is not None for d in docs.values() for v in d["fields"].values())
        confident = all((d["ocr"] or {}).get("confidence", 0) >= config.ocr_trust_min()
                        or d["read_method"] == "vision"
                        for k, d in docs.items() if k in scanned)
        if not (config.ocr_policy() == "trust" and complete and confident):
            which = " and ".join(scanned)
            return _escalate(email_id,
                             f"Could not read attachment ({which}): image-only scan; values read by "
                             f"{'/'.join(sorted({docs[k]['read_method'] for k in scanned}))} "
                             f"need human confirmation",
                             retryable=retryable, **fields, **evidence)

    # --- blanks / missing values ---------------------------------------------------------
    si_blanks, bl_blanks = docs["SI"]["blanks"], docs["BL"]["blanks"]
    if si_blanks or bl_blanks:
        return _escalate(email_id, f"Field extraction failed - blank value(s) in document: "
                                   f"SI {si_blanks}, BL {bl_blanks}",
                         retryable=retryable, **fields, **evidence)

    si_missing = [k for k, v in si_fields.items() if v is None]
    bl_missing = [k for k, v in bl_fields.items() if v is None]
    if si_missing or bl_missing:
        return _escalate(email_id, f"Field extraction failed - SI missing: {si_missing}, "
                                   f"BL missing: {bl_missing}",
                         retryable=retryable, **fields, **evidence)

    return {"email_id": email_id, "status": "ok", "retryable": retryable, **fields, **evidence}
