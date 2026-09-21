"""
batch.py — turn uploaded files into email records + attachments, for
"Upload & Check" with many emails at once.

Accepted uploads (mix freely, several at once):
  .zip    an inbox bundle in the dataset layout (inbox/*.json + attachments/),
          or any zip containing email .json / .eml files and attachment files
  .json   one email record, a list of records, or {"emails": [...]}
  .eml    a real email (Gmail / Outlook / Apple Mail "save as"), with its
          attachments inside
  other   attachment files (txt / pdf / docx / xlsx / images), matched to the
          emails that reference them by file name

Nothing is extracted to disk from a zip: entries are read into memory with
size limits, so zip path tricks ("../../etc") can't escape. ground_truth.json
and sample_submission.json inside a bundle are ignored.

  parsed = parse_uploads([(filename, bytes), ...])
  -> Parsed(emails=[...], attachments={name: bytes}, warnings=[...])
"""
import email as email_lib
import email.policy
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import config
from .uploads import ALLOWED_EXTENSIONS, UploadError

IGNORED_JSON = {"ground_truth.json", "sample_submission.json"}
MAX_ZIP_ENTRIES = 5000


@dataclass
class Parsed:
    emails: list = field(default_factory=list)
    attachments: dict = field(default_factory=dict)     # basename -> bytes
    warnings: list = field(default_factory=list)


def max_batch_bytes():
    return config.max_batch_mb() * 1024 * 1024


def safe_name(name):
    """Basename only, restricted characters - used for storage and matching."""
    base = PurePosixPath(str(name).replace("\\", "/")).name
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)[:120] or "file"


def _add_attachment(parsed, name, data):
    ext = PurePosixPath(name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        parsed.warnings.append(f"skipped {name}: unsupported file type")
        return None
    key = safe_name(name)
    if key in parsed.attachments and parsed.attachments[key] != data:
        parsed.warnings.append(f"two different files named {key}; kept the first")
        return key
    parsed.attachments.setdefault(key, data)
    return key


# Freestyle records: judges may improvise on the sample format. Keys are matched
# case-insensitively, and these aliases map onto the spec's fields.
_ALIASES = {
    "email_id": ("email_id", "id", "emailid", "message_id", "messageid"),
    "from": ("from", "sender", "from_email", "from_address"),
    "subject": ("subject", "title"),
    "body": ("body", "text", "content", "message", "body_text"),
    "attachments": ("attachments", "files", "attachment", "documents"),
}


def _pick(lowered, field_name):
    for key in _ALIASES[field_name]:
        if key in lowered and lowered[key] not in (None, ""):
            return lowered[key]
    return None


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(_as_text(v) for v in value)
    if isinstance(value, dict):            # e.g. {"name": ..., "email": ...}
        return str(value.get("email") or value.get("address") or value.get("name") or "")
    return str(value)


def _attachment_names(value):
    """Paths, bare file names, or objects like {"filename": ...} / {"path": ...}."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    names = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("filename") or item.get("name") or item.get("path") or item.get("file")
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
    return names


def normalize_record(rec, fallback_id):
    """Any reasonable email dict -> the spec's shape:
    {email_id, from, subject, body, attachments}. None if it isn't an email."""
    if not isinstance(rec, dict):
        return None
    lowered = {str(k).strip().lower(): v for k, v in rec.items()}
    if not any(_pick(lowered, f) is not None for f in ("from", "subject", "body", "attachments")):
        return None
    email_id = _as_text(_pick(lowered, "email_id")).strip() or fallback_id
    return {
        "email_id": email_id,
        "from": _as_text(_pick(lowered, "from")).strip(),
        "subject": _as_text(_pick(lowered, "subject")).strip(),
        "body": _as_text(_pick(lowered, "body")),
        "attachments": _attachment_names(_pick(lowered, "attachments")),
    }


def _emails_from_json(data, source_name, parsed):
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed.warnings.append(f"skipped {source_name}: not valid JSON")
        return
    if isinstance(obj, dict) and isinstance(obj.get("emails"), list):
        items = obj["emails"]
    elif isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        items = [obj]
    else:
        items = []
    stem = PurePosixPath(source_name).stem
    added = 0
    for i, rec in enumerate(items):
        fallback = stem if len(items) == 1 else f"{stem}_{i + 1}"
        rec = normalize_record(rec, fallback)
        if rec is None:
            continue
        parsed.emails.append(rec)
        added += 1
    if not added:
        parsed.warnings.append(f"skipped {source_name}: no email records found")


def _email_from_eml(data, source_name, parsed):
    msg = email_lib.message_from_bytes(data, policy=email.policy.default)
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        try:
            body = body_part.get_content()
        except Exception:
            body = ""
        if body_part.get_content_type() == "text/html":
            body = re.sub(r"<[^>]+>", " ", body)
    attachments = []
    for part in msg.iter_attachments():
        name = part.get_filename()
        payload = part.get_payload(decode=True)
        if name and payload:
            key = _add_attachment(parsed, name, payload)
            if key:
                attachments.append(key)
    parsed.emails.append({
        "email_id": safe_name(PurePosixPath(source_name).stem),
        "from": str(msg.get("from") or ""),
        "subject": str(msg.get("subject") or ""),
        "body": body.strip(),
        "attachments": attachments,
    })


def _handle(name, data, parsed, depth=0):
    base = PurePosixPath(name.replace("\\", "/")).name
    ext = PurePosixPath(base).suffix.lower()
    if not base or base.startswith(".") or "__MACOSX" in name:
        return
    if base in IGNORED_JSON:
        parsed.warnings.append(f"ignored {base}")
        return
    if ext != ".zip" and data[:4] == b"PK\x03\x04" and ext not in (".docx", ".xlsx"):
        ext = ".zip"          # a zip that lost its extension (docx/xlsx are zips too - keep those)
    if ext == ".zip":
        if depth > 0:
            parsed.warnings.append(f"skipped nested zip {base}")
            return
        _handle_zip(base, data, parsed)
    elif ext == ".json":
        _emails_from_json(data, base, parsed)
    elif ext == ".eml":
        _email_from_eml(data, base, parsed)
    else:
        _add_attachment(parsed, base, data)


def _handle_zip(name, data, parsed):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise UploadError(f"{name} is not a valid zip file")
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_ZIP_ENTRIES:
        raise UploadError(f"{name} has too many files ({len(infos)} > {MAX_ZIP_ENTRIES})")
    total = sum(i.file_size for i in infos)
    if total > max_batch_bytes():
        raise UploadError(f"{name} unpacks to {total // 1_000_000} MB, over the "
                          f"{config.max_batch_mb()} MB limit")
    for info in infos:
        with zf.open(info) as fh:
            content = fh.read(max_batch_bytes() + 1)
        _handle(info.filename, content, parsed, depth=1)


def parse_uploads(files):
    """files: [(filename, bytes)]. Raises UploadError if nothing usable."""
    if sum(len(d) for _, d in files) > max_batch_bytes():
        raise UploadError(f"Upload is over the {config.max_batch_mb()} MB limit")
    parsed = Parsed()
    for name, data in files:
        _handle(str(name), data, parsed)

    if not parsed.emails:
        raise UploadError("No emails found. Upload a zip of an inbox (inbox/*.json + attachments/), "
                          "email .json files, or .eml files.")
    if len(parsed.emails) > config.max_batch_emails():
        raise UploadError(f"{len(parsed.emails)} emails - the limit is {config.max_batch_emails()} per batch")

    seen = {}
    for rec in parsed.emails:                       # keep ids unique
        eid = str(rec["email_id"])
        seen[eid] = seen.get(eid, 0) + 1
        if seen[eid] > 1:
            rec["email_id"] = f"{eid}_{seen[eid]}"
            parsed.warnings.append(f"duplicate email_id {eid}; renamed to {rec['email_id']}")

    referenced = {safe_name(a) for r in parsed.emails for a in r["attachments"] if isinstance(a, str)}
    missing = sorted(referenced - set(parsed.attachments))
    if missing:
        parsed.warnings.append(f"{len(missing)} referenced attachment(s) not uploaded, e.g. "
                               f"{', '.join(missing[:3])} - those emails will be flagged")
    return parsed
