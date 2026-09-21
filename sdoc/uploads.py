"""
uploads.py — "Upload & Check": run the pipeline on an email someone submits
through the web UI (e.g. a judge testing their own SI / BL documents).

  email, error = save_upload(subject, sender, body, si=(name, bytes), bl=(name, bytes))
  inbox = UploadInbox(Inbox(config.data_source()))   # reads dataset + uploads
  record = pipeline.process_one(email, inbox)
  append_record(record)                              # shows up in inbox / review

Safety:
  * files are stored under server-generated names (never the uploader's
    filename -> no path traversal), inside SDOC_OUT/uploads/<email_id>/
  * extension allowlist + size limit (SDOC_MAX_UPLOAD_MB)
  * uploaded emails are kept out of submission.json, which stays exactly the
    dataset's shape for the self-evaluation
"""
import json
import secrets
import time
from pathlib import Path

from . import config

ALLOWED_EXTENSIONS = {".txt", ".pdf", ".docx", ".xlsx",
                      ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
PREFIX = "uploads/"
# matches a classifier comparison phrase, so an upload with files but no email
# text is routed straight to the SI/BL comparison (no AI needed to classify it)
DEFAULT_BODY = "Please compare the SI and draft BL and confirm."


class UploadError(ValueError):
    pass


class UploadInbox:
    """Wraps the dataset Inbox; attachment paths starting with "uploads/" are
    read from the uploads folder instead."""

    def __init__(self, base):
        self.base = base

    def _upload_path(self, att_path):
        root = config.uploads_dir().resolve()
        path = (root / att_path[len(PREFIX):]).resolve()
        if root not in path.parents:
            raise FileNotFoundError(att_path)
        return path

    def read_bytes(self, att_path):
        if str(att_path).startswith(PREFIX):
            return self._upload_path(att_path).read_bytes()
        return self.base.read_bytes(att_path)

    def read_text(self, att_path, encoding="utf-8"):
        return self.read_bytes(att_path).decode(encoding, errors="replace")

    def __getattr__(self, name):          # emails(), get(), submit(), ...
        return getattr(self.base, name)


def _clean_ext(filename):
    ext = Path(str(filename or "")).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadError(f"Unsupported file type {ext or '(none)'}; allowed: "
                          f"{', '.join(sorted(ALLOWED_EXTENSIONS))}")
    return ext


def save_upload(subject="", sender="", body="", si=None, bl=None):
    """si / bl: (original_filename, bytes) or None. Returns the email record."""
    subject, sender, body = (str(x or "").strip() for x in (subject, sender, body))
    limit = config.max_upload_mb() * 1024 * 1024
    files = {k: v for k, v in (("SI", si), ("BL", bl)) if v and v[1]}
    if not files and not body and not subject:
        raise UploadError("Add an email text and/or an SI and BL document")
    for kind, (_, data) in files.items():
        if len(data) > limit:
            raise UploadError(f"{kind} file is larger than {config.max_upload_mb()} MB")

    email_id = f"upload_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"
    folder = config.uploads_dir() / email_id
    folder.mkdir(parents=True, exist_ok=True)
    attachments = []
    for kind, (name, data) in files.items():
        stored = f"{email_id}_{kind}{_clean_ext(name)}"
        (folder / stored).write_bytes(data)
        attachments.append(f"{PREFIX}{email_id}/{stored}")

    return {
        "email_id": email_id,
        "from": sender or "upload@web",
        "subject": subject or "Uploaded documents",
        "body": body or (DEFAULT_BODY if files else ""),
        "attachments": attachments,
    }


def _records_file():
    return config.out_dir() / "uploads.json"


def load_records():
    try:
        return json.loads(_records_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_batch(parsed):
    """Store a parsed batch (batch.parse_uploads) under uploads/<batch_id>/.
    Returns (batch_id, emails) where each email has an internal id
    "<batch_id>__<original id>", attachment paths pointing at the stored files,
    and "original_id" for the downloadable submission. Attachments an email
    references but that weren't uploaded keep their (non-existent) path: the
    file name still tells the classifier it's an SI/BL email, and extraction
    reports it as a missing attachment."""
    from .batch import safe_name
    batch_id = f"batch_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"
    folder = config.uploads_dir() / batch_id
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in parsed.attachments.items():
        (folder / name).write_bytes(data)
    emails, used = [], set()
    for rec in parsed.emails:
        atts = []
        for a in rec.get("attachments") or []:
            key = safe_name(a) if isinstance(a, str) else None
            if key:
                atts.append(f"{PREFIX}{batch_id}/{key}")
        internal = f"{batch_id}__{safe_name(rec['email_id'])}"      # URL-safe
        while internal in used:
            internal += "_"
        used.add(internal)
        emails.append({**rec, "email_id": internal,
                       "original_id": str(rec["email_id"]), "attachments": atts})
    return batch_id, emails


def _meta_path(batch_id):
    return config.uploads_dir() / batch_id / "_batch.json"


def write_batch_meta(batch_id, **fields):
    """Remember a batch's size and whether it finished, on disk: after a server
    restart this tells a finished batch apart from one that was interrupted."""
    path = _meta_path(batch_id)
    meta = read_batch_meta(batch_id) or {}
    meta.update(fields)
    try:
        path.write_text(json.dumps(meta), encoding="utf-8")
    except OSError:
        pass


def read_batch_meta(batch_id):
    try:
        return json.loads(_meta_path(batch_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def append_record(record):
    append_records([record])


def append_records(new_records):
    records = load_records()
    records.extend({**r, "source": "upload"} for r in new_records)
    path = _records_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Deleting (everything linked goes: record, attachment files, review decision)
# ---------------------------------------------------------------------------
def _write_records(records):
    path = _records_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(records, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def _safe_upload_path(att_path):
    """uploads/<folder>/<file> -> absolute path inside the uploads folder, or None."""
    if not isinstance(att_path, str) or not att_path.startswith(PREFIX):
        return None
    root = config.uploads_dir().resolve()
    path = (root / att_path[len(PREFIX):]).resolve()
    return path if root in path.parents else None


def _folder_of(record):
    """The uploads/ sub-folder that holds this record's files."""
    return record.get("batch_id") or record.get("email_id")


def _drop_decisions(email_ids):
    path = config.out_dir() / "review_decisions.json"
    try:
        decisions = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return 0
    removed = [e for e in email_ids if e in decisions]
    if removed:
        for e in removed:
            decisions.pop(e)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(decisions, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(path)
    return len(removed)


def _delete_files(doomed, keep):
    """Delete the attachment files of `doomed` records that no `keep` record still
    uses; remove folders that end up empty. Returns the number of files deleted."""
    import shutil
    still_used = {a for r in keep for a in r.get("attachments") or []}
    deleted, folders = 0, set()
    for rec in doomed:
        folders.add(_folder_of(rec))
        for att in rec.get("attachments") or []:
            if att in still_used:
                continue
            path = _safe_upload_path(att)
            if path and path.is_file():
                path.unlink()
                deleted += 1
    root = config.uploads_dir().resolve()
    remaining_folders = {_folder_of(r) for r in keep}
    for folder in folders:
        if not folder or folder in remaining_folders:
            continue
        path = (root / folder).resolve()
        if root in path.parents and path.is_dir():
            deleted += sum(1 for p in path.rglob("*") if p.is_file() and p.name != "_batch.json")
            shutil.rmtree(path)
    return deleted


def _delete_where(predicate):
    records = load_records()
    doomed = [r for r in records if predicate(r)]
    if not doomed:
        return None
    keep = [r for r in records if not predicate(r)]
    _write_records(keep)
    files = _delete_files(doomed, keep)
    decisions = _drop_decisions([r["email_id"] for r in doomed])
    return {"emails": len(doomed), "files": files, "decisions": decisions,
            "ids": [r["email_id"] for r in doomed]}


def delete_email(email_id):
    """Delete one uploaded email (single upload, or one email of a batch)."""
    return _delete_where(lambda r: r.get("email_id") == email_id)


def delete_batch(batch_id):
    """Delete every email of a batch, its stored files and folder."""
    return _delete_where(lambda r: r.get("batch_id") == batch_id)


def delete_all():
    """Delete every upload. Also sweeps stray folders (e.g. an upload that was
    stored but never processed)."""
    import shutil
    result = _delete_where(lambda r: True) or {"emails": 0, "files": 0, "decisions": 0, "ids": []}
    root = config.uploads_dir()
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir():
                result["files"] += sum(1 for p in child.rglob("*") if p.is_file() and p.name != "_batch.json")
                shutil.rmtree(child)
    return result
