"""
document_reader.py — turn ANY attachment (txt / pdf / docx / xlsx / scanned
PDF / image) into one JSON-ready "document record" for the pipeline.

    record = read_document(path, inbox, kind="SI" | "BL")

How a file is read:
  txt              -> decode
  pdf              -> text layer (font-aware); if there is no text layer it is
                      a scan -> OCR (-> vision model if OCR is weak/missing)
  docx / xlsx      -> table-aware readers
  png/jpg/tif...   -> OCR (-> vision fallback)

How fields are found (SDOC_EXTRACTION_MODE):
  "ai"    (default) PDF/DOCX/XLSX/scans are analysed by the AI; the rule-based
          parser runs too, as an independent cross-check. txt: rules first.
  "auto"  rules first everywhere, AI only for fields the rules can't find
  "rules" never call the AI
If the AI is unavailable the record falls back to the rules and says so in
"warnings" (the run still completes, and the email is marked retryable).

Trust rules:
  * An AI value is only accepted if it can be found in the document text
    (exact, or fuzzy for OCR text). Ungrounded values are dropped: no
    hallucinated shipper names reach the comparison.
  * Rules and AI agree -> source "rules+ai". Disagree -> the labelled
    rules value wins for text documents, the AI value (OCR-corrected) wins
    for scans; the disagreement is recorded for the reviewer.

Record shape (all JSON-serialisable):
  {path, kind, file_type, read_method, document_type, document_type_source,
   fields{7}, field_sources{}, evidence{}, blanks[], disagreements{},
   ocr{engine, confidence}|None, text_preview, warnings[],
   error_code|None, error|None, retryable}
error_code: unsupported_format | read_failed | corrupt_file | no_text | ocr_unavailable
"""
import re
from difflib import SequenceMatcher

from . import activity, ai_extraction, compare, config, disk_cache, llm, ocr
from .format_readers import get_file_type, read_docx_text, read_pdf_text, read_xlsx_text
from .label_matching import (FIELD_NAMES, extract_all_fields_detailed, extract_container_count,
                            extract_number)

MIN_TEXT_CHARS = 20
OCR_MIN_CONFIDENCE = 0.60      # below this, try the vision model instead
OCR_VERSION = "ocr-v1"

FOREIGN_DOC_TITLES = {
    "COMMERCIAL INVOICE": "COMMERCIAL_INVOICE",
    "PROFORMA INVOICE": "COMMERCIAL_INVOICE",
    "PACKING LIST": "PACKING_LIST",
    "CERTIFICATE OF ORIGIN": "CERTIFICATE_OF_ORIGIN",
}
SI_MARKERS = ["SHIPPING INSTRUCTION", "BILL OF LADING", "BL INSTRUCTION", "B/L INSTRUCTION"]
BL_MARKERS = ["BILL OF LADING", "B/L NO", "B/L NUMBER"]


def extraction_mode():
    return config.extraction_mode()


# ---------------------------------------------------------------------------
# Document type
# ---------------------------------------------------------------------------
def detect_document_type(text, kind):
    """Rule-based: returns (doc_type or None). None = no confident opinion.
    Only calls something foreign when a foreign title is present AND the
    expected SI/BL markers are absent, so an unusual real SI/BL is never rejected."""
    up = str(text).upper()
    markers = BL_MARKERS if kind == "BL" else SI_MARKERS
    if any(m in up for m in markers):
        return "BILL_OF_LADING" if kind == "BL" else "SHIPPING_INSTRUCTION"
    for title, doc_type in FOREIGN_DOC_TITLES.items():
        if title in up:
            return doc_type
    if kind == "BL" and "SHIPPING INSTRUCTION" in up:
        return "SHIPPING_INSTRUCTION"          # a second SI attached in the BL slot
    return None


def is_wrong_type(doc_type, kind):
    if doc_type is None or doc_type == "OTHER":
        return False
    if kind == "BL":
        return doc_type != "BILL_OF_LADING"
    return doc_type in ("COMMERCIAL_INVOICE", "PACKING_LIST", "CERTIFICATE_OF_ORIGIN")


# ---------------------------------------------------------------------------
# Grounding: is an AI value really in the document?
# ---------------------------------------------------------------------------
def _alnum(s):
    return re.sub(r"[^a-z0-9]", "", str(s).casefold())


def _best_window_ratio(needle, haystack):
    n = len(needle)
    if not n or len(haystack) < n:
        return SequenceMatcher(None, needle, haystack).ratio() if haystack else 0.0
    best = 0.0
    for i in range(0, len(haystack) - n + 1):
        r = SequenceMatcher(None, needle, haystack[i:i + n]).ratio()
        if r > best:
            best = r
            if best == 1.0:
                break
    return best


def _grounded(field, value, text, fuzzy):
    hay = _alnum(text)
    if field in ("container_count", "gross_weight_kg"):
        num = extract_container_count(value) if field == "container_count" else extract_number(value)
        if num is None:
            return False
        digits = str(int(num)) if float(num).is_integer() else str(num).replace(".", "")
        return digits in re.sub(r"[^0-9]", "", str(text)) or digits in hay
    needle = _alnum(value)
    if not needle:
        return False
    if needle in hay:
        return True
    return fuzzy and _best_window_ratio(needle, hay) >= 0.80


def _normalise_value(field, value):
    if value is None:
        return None
    if field == "container_count":
        return extract_container_count(value)
    if field == "gross_weight_kg":
        return extract_number(value)
    return " ".join(str(value).split())


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def _base_record(path, kind):
    return {"path": path, "kind": kind, "file_type": get_file_type(path), "read_method": None,
            "document_type": None, "document_type_source": None,
            "fields": {f: None for f in FIELD_NAMES}, "field_sources": {}, "evidence": {},
            "blanks": [], "disagreements": {}, "ocr": None, "text_preview": "",
            "warnings": [], "error_code": None, "error": None, "retryable": False}


def _fail(rec, code, message, retryable=False):
    rec.update(error_code=code, error=message, retryable=retryable)
    return rec


def _ocr_images(rec, images, cache_bytes):
    """OCR (cached). Returns text or None; fills rec['ocr']."""
    key = disk_cache.key_for(cache_bytes, OCR_VERSION)
    cached = disk_cache.get(key)
    if not cached:
        activity.step(f"Scanned document - reading the {rec['kind']} with OCR",
                      detail=f"{len(images)} page(s); this can take a few seconds per page", mode="ocr")
    result = cached or ocr.ocr_pages(images)
    if not cached:
        disk_cache.put(key, result)
    rec["ocr"] = {"engine": result["engine"], "confidence": result["confidence"]}
    return result["text"]


def _read_text(rec, data):
    """Fill rec['read_method'] and return the document text, or None on failure."""
    ftype = rec["file_type"]
    if ftype == "txt":
        rec["read_method"] = "text"
        return data.decode("utf-8", errors="replace")
    if ftype in ("docx", "xlsx"):
        rec["read_method"] = ftype
        return (read_docx_text if ftype == "docx" else read_xlsx_text)(data)

    # pdf (text layer or scan) and images: may need OCR
    images = None
    if ftype == "pdf":
        text = read_pdf_text(data)
        if len(text.strip()) >= MIN_TEXT_CHARS:
            rec["read_method"] = "pdf_text"
            return text
        images = ocr.render_pdf_pages(data)
    else:
        images = [ocr.load_image(data)]

    rec["_images"] = images                # kept in memory for the vision fallback only
    rec["read_method"] = "ocr"
    try:
        return _ocr_images(rec, images, data)
    except ocr.OCRUnavailable as e:
        rec["warnings"].append(str(e))
        rec["_ocr_unavailable"] = True
        return ""


def read_document(path, inbox, kind):
    rec = _base_record(path, kind)
    if rec["file_type"] == "unknown":
        return _fail(rec, "unsupported_format", f"Unsupported attachment format: {path}")

    try:
        data = inbox.read_bytes(path)
    except FileNotFoundError as e:
        return _fail(rec, "read_failed", f"Attachment not found: {e}")
    except Exception as e:                       # e.g. server/network hiccup
        return _fail(rec, "read_failed", f"{type(e).__name__}: {e}", retryable=True)
    if not data:
        return _fail(rec, "no_text", "Empty file (0 bytes)")

    activity.step(f"Reading the {kind} ({rec['file_type'].upper()})", detail=path.rsplit("/", 1)[-1],
                  mode="local")
    try:
        text = _read_text(rec, data)
    except Exception as e:
        return _fail(rec, "corrupt_file", f"File could not be opened ({type(e).__name__}: {e})")

    mode = extraction_mode()
    images = rec.pop("_images", None)
    ocr_missing = rec.pop("_ocr_unavailable", False)
    is_scan = rec["read_method"] == "ocr"

    # Weak or missing OCR -> vision model on the page images
    ai_result, ai_error = None, None
    weak_ocr = is_scan and (ocr_missing or len(text.strip()) < MIN_TEXT_CHARS
                            or (rec["ocr"] and rec["ocr"]["confidence"] < OCR_MIN_CONFIDENCE))
    if weak_ocr and images and mode != "rules":
        if llm.availability()[0]:
            activity.step(f"OCR couldn't read the {kind} well - the vision AI is looking at the page",
                          mode="ai")
        ai_result, ai_error = ai_extraction.analyze_document_images(images)
        if ai_result:
            rec["read_method"] = "vision"
            text = text or ""
    if weak_ocr and not ai_result:
        if ocr_missing:
            return _fail(rec, "ocr_unavailable",
                         "Image-only scan and no OCR engine installed", retryable=True)
        if len(text.strip()) < MIN_TEXT_CHARS:
            return _fail(rec, "no_text", "No readable text (image-only scan, OCR found nothing)")

    if rec["read_method"] != "vision" and len(text.strip()) < MIN_TEXT_CHARS:
        return _fail(rec, "no_text", "No extractable text (empty file or image-only scan)")
    rec["text_preview"] = text[:600]

    # --- rules pass (always: it is the cross-check) ------------------------
    rule_fields, rule_blanks = extract_all_fields_detailed(text)
    rec["document_type"] = detect_document_type(text, kind)
    rec["document_type_source"] = "rules" if rec["document_type"] else None

    # --- AI pass -------------------------------------------------------------
    wants_ai = mode != "rules" and ai_result is None and (
        is_scan
        or (mode == "ai" and rec["file_type"] != "txt")
        or any(v is None and f not in rule_blanks for f, v in rule_fields.items())
    )
    if wants_ai and llm.availability()[0]:
        activity.step(f"The AI is reading the {kind}",
                      detail="cross-checking the rule-based reading" if not is_scan else "cleaning up the OCR text",
                      mode="ai")
    if wants_ai:
        ai_result, ai_error = ai_extraction.analyze_document_text(text, source="ocr" if is_scan else "text")
    if ai_error and ai_error not in ("ai_disabled", "ai_not_configured"):
        rec["warnings"].append(f"AI analysis failed ({ai_error}); used the rule-based reading only")
        rec["retryable"] = ai_error in ai_extraction.RETRYABLE_ERRORS
        rec["ai_error"] = ai_error

    # --- merge ----------------------------------------------------------------
    ai_fields = ai_result["fields"] if ai_result else {}
    ai_evidence = ai_result.get("evidence", {}) if ai_result else {}
    ai_blanks = set(ai_result.get("blank_fields", [])) if ai_result else set()
    prefer_ai = is_scan or rec["read_method"] == "vision"
    blanks = set(rule_blanks)

    for f in FIELD_NAMES:
        r = _normalise_value(f, rule_fields.get(f))
        a = _normalise_value(f, ai_fields.get(f))
        if a is not None and rec["read_method"] != "vision" and not _grounded(f, a, text, fuzzy=is_scan):
            rec["warnings"].append(f"AI value for {f} not found in the document; ignored ({a!r})")
            a = None

        if r is not None and a is not None:
            agree, _ = compare.fields_match(f, r, a)
            if agree:
                rec["fields"][f], rec["field_sources"][f] = (a if prefer_ai else r), "rules+ai"
            else:
                rec["fields"][f] = a if prefer_ai else r
                rec["field_sources"][f] = "ai" if prefer_ai else "rules"
                rec["disagreements"][f] = {"rules": r, "ai": a}
        elif r is not None:
            rec["fields"][f], rec["field_sources"][f] = r, "rules"
        elif a is not None:
            rec["fields"][f], rec["field_sources"][f] = a, rec["read_method"] if rec["read_method"] == "vision" else "ai"
        elif f in ai_blanks:
            blanks.add(f)                      # AI saw the label but the value is blank
        if rec["fields"][f] is not None:
            blanks.discard(f)
        if f in ai_evidence and rec["fields"][f] is not None:
            rec["evidence"][f] = ai_evidence[f]

    rec["blanks"] = sorted(blanks)

    # document type: rules win when they have an opinion; the AI only fills in
    if rec["document_type"] is None and ai_result and ai_result.get("document_type") not in (None, "OTHER"):
        rec["document_type"] = ai_result["document_type"]
        rec["document_type_source"] = "ai"
    return rec
