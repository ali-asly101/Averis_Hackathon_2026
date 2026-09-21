"""
email_utils.py — small helpers shared by classify.py and extraction.py, so
the two stages always agree with each other.

  find_si_bl_attachments(attachments) -> (si_files, bl_files)
  clean_email_body(body)              -> the sender's own message only
  is_send_bl_request(email)           -> "please send the draft BL" (nothing to compare yet)
"""
import re

# ---------------------------------------------------------------------------
# SI / BL attachment detection
# ---------------------------------------------------------------------------
# A raw `"_SI" in path` also matches "contract_SIGNED.pdf" / "_SITE", and
# `"_BL"` matches "_BLANK" / "_BLUE". SI/BL must be a whole token in the
# filename stem, delimited by _ - . space or the start/end. Any extension.
_SI_TOKEN_RE = re.compile(r"(?:^|[_\-\s.])SI(?=[_\-\s.]|$)", re.IGNORECASE)
_BL_TOKEN_RE = re.compile(r"(?:^|[_\-\s.])BL(?=[_\-\s.]|$)", re.IGNORECASE)


def _stem(path):
    name = re.split(r"[\\/]", str(path))[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def find_si_bl_attachments(attachments):
    """Return (si_files, bl_files). Non-string entries are ignored."""
    si_files, bl_files = [], []
    for path in attachments if isinstance(attachments, list) else []:
        if not isinstance(path, str) or not path.strip():
            continue
        stem = _stem(path)
        if _SI_TOKEN_RE.search(stem):
            si_files.append(path)
        if _BL_TOKEN_RE.search(stem):
            bl_files.append(path)
    return si_files, bl_files


# ---------------------------------------------------------------------------
# Email body cleaning
# ---------------------------------------------------------------------------
# Forwarded emails carry an external-sender banner ("...caution with E-Mail
# content and any links or attachments") and the older thread underneath.
# Keyword checks must look at what THIS sender wrote, not at boilerplate.
_BANNER_RE = re.compile(
    r"^\s*(?:WARNING|CAUTION|\[?EXTERNAL\]?)\b[^\n]*(?:originated outside|external sender|"
    r"outside of (?:our|the) organi[sz]ation)[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
_THREAD_SPLIT_RE = re.compile(
    r"^\s*(?:_{5,}|-{5,}\s*Original Message\s*-{5,}|From:\s)", re.IGNORECASE | re.MULTILINE
)


def clean_email_body(body):
    text = "" if body is None else str(body)
    text = _BANNER_RE.sub("", text)
    parts = _THREAD_SPLIT_RE.split(text, maxsplit=1)
    return parts[0].strip()


# ---------------------------------------------------------------------------
# "Please send the draft BL" (no documents yet)
# ---------------------------------------------------------------------------
_SEND_BL_RE = re.compile(r"\b(?:send|provide|share|forward)\b[^.\n]{0,40}\bdraft\s*b/?l\b",
                         re.IGNORECASE)
_COMPARE_OR_ATTACHED_RE = re.compile(r"\bcompare\b|\battach", re.IGNORECASE)


def is_send_bl_request(email):
    """True when the sender is asking for the draft BL to be SENT, and is not
    claiming anything is attached / asking for a comparison. Such an email
    is a BL_COMPARISON request with nothing to compare yet -> status OK,
    not NEEDS_REVIEW (matches the ground-truth convention)."""
    if not isinstance(email, dict):
        return False
    own_text = clean_email_body(email.get("body"))
    return bool(_SEND_BL_RE.search(own_text)) and not _COMPARE_OR_ATTACHED_RE.search(own_text)
