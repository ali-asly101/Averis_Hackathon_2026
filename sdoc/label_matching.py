"""
label_matching.py — rule-based field extraction from SI/BL text.

Rules:
  * A label must EXACTLY match a known label once noise is removed
    (parenthetical groups like "(POL)" / "(发货人)", non-ASCII text like
    "毛重", case, extra spaces). No fuzzy matching: a near-miss label returns
    None and the AI fallback handles it, instead of confidently grabbing the
    wrong field (e.g. "Container No." is a container serial, not a count).
  * Label list = the label variants actually used in the data (pools.py in
    data_v2) — every entry is backed by evidence, not guessed.
  * Works with "Label: value" lines (txt/docx/xlsx) AND "Label value" lines
    with no colon (the PDF layout).
  * Placeholders ("N/A", "???", "____", "TBA") mean the document left the
    field blank. They are reported separately as blanks — a blank is
    "can't compare" (NEEDS_REVIEW), not a value and not a mismatch.
  * If one field shows up with conflicting values, it returns None so the
    AI (which sees the whole document) decides instead of trusting line order.
"""
import re

FIELD_NAMES = [
    "shipper", "consignee", "notify_party", "port_of_loading",
    "port_of_discharge", "container_count", "gross_weight_kg",
]
NUMERIC_FIELDS = ("container_count", "gross_weight_kg")

# Normalised labels (see normalize_label). Sourced from the label pools the
# data generator uses, which mirror the real APRIL documents.
FIELD_LABELS = {
    "shipper": ["shipper", "shipper/exporter"],
    "consignee": ["consignee", "to the order of"],
    "notify_party": ["notify party", "notify", "notify party/intermediate consignee"],
    "port_of_loading": ["port of loading", "load port", "pol"],
    "port_of_discharge": ["port of discharge", "discharge port", "pod"],
    "container_count": ["container count", "total containers", "no. of containers",
                        "no. of containers or packages", "containers"],
    "gross_weight_kg": ["gross weight", "gross wt", "total gross weight", "total gross wt"],
}

# O(1) lookup: normalised label -> field
_LABEL_TO_FIELD = {lbl: field for field, lbls in FIELD_LABELS.items() for lbl in lbls}

_PLACEHOLDER_RE = re.compile(
    r"^(?:[_?.\-*\s/]+|n\s*/?\s*a|tba|tbc|tbd|nil|none|null|unknown|pending)$",
    re.IGNORECASE,
)
_UNIT_SUFFIX_RE = re.compile(r"\s*(?:kgs?|mts?|kilos?|tons?)\.?\s*$", re.IGNORECASE)


def normalize_label(label):
    """"Gross Wt (kgs) (毛重 KGS)" -> "gross wt"; "Shipper (Principal or Seller)" -> "shipper"."""
    s = re.sub(r"\([^)]*\)", " ", str(label))       # drop (...) groups
    s = re.sub(r"[^\x00-\x7F]+", " ", s)             # drop non-ASCII (bilingual labels)
    s = " ".join(s.lower().split())
    return s.strip(" :")


def is_label_match(label, label_part):
    """Kept for compatibility: exact match after normalisation."""
    return normalize_label(label) == normalize_label(label_part)


def is_placeholder(value):
    if value is None:
        return False
    s = _UNIT_SUFFIX_RE.sub("", str(value)).strip()
    return s == "" or bool(_PLACEHOLDER_RE.match(s))


def _clean_value(value):
    """xlsx packs "NAME | address; address" into one cell: keep the name."""
    v = str(value).split(" | ")[0]
    return " ".join(v.split()).strip(" ;")


def extract_number(raw_value):
    """First number in the string -> int (or float if it has decimals).
    "131,058 KG" -> 131058, "Approx, 131,058 KG" -> 131058 (the old version
    crashed on this), "N/A" -> None."""
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        return raw_value
    match = re.search(r"\d[\d,]*(?:\.\d+)?", str(raw_value))
    if not match:
        return None
    num = float(match.group().replace(",", ""))
    return int(num) if num.is_integer() else num


def extract_container_count(raw_value):
    """"6 x 40'HC" -> 6, "3 x 20'GP + 4 x 40'HC" -> 7, "40'HC x 6" -> 6."""
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        return int(raw_value)
    s = str(raw_value)
    qty_first = re.findall(r"(\d+)\s*[xX×]\s*\d{2}\s*'", s)
    if qty_first:
        return sum(int(q) for q in qty_first)
    size_first = re.findall(r"\d{2}\s*'\s*[A-Za-z]*\s*[xX×]\s*(\d+)", s)
    if size_first:
        return sum(int(q) for q in size_first)
    n = extract_number(s)
    return int(n) if n is not None else None


def _prefix_pattern(label):
    words = [re.escape(w) for w in label.split()]
    return re.compile(r"^\s*" + r"\s+".join(words) + r"(?:\s*\([^)]*\))*\s+(\S.*)$",
                      re.IGNORECASE)


# Longest labels first, so "notify party/intermediate consignee" wins over "notify".
_PREFIX_PATTERNS = [
    (_prefix_pattern(lbl), _LABEL_TO_FIELD[lbl])
    for lbl in sorted(_LABEL_TO_FIELD, key=len, reverse=True)
]


def _match_line(line, next_line):
    """Return (field, raw_value) for one line, or (None, None)."""
    if ":" in line:
        label_part, value_part = line.split(":", 1)
        field = _LABEL_TO_FIELD.get(normalize_label(label_part))
        if field is None:
            return None, None
        value = value_part.strip()
        if not value and next_line and ":" not in next_line:
            value = next_line.strip()   # "Shipper:\nAPRIL FAR EAST ..."
        return field, value
    for pattern, field in _PREFIX_PATTERNS:   # PDF layout: "Load Port BUATAN, INDONESIA"
        m = pattern.match(line)
        if m:
            return field, m.group(1).strip()
    return None, None


def _dedupe_key(field, value):
    if field == "container_count":
        return extract_container_count(value)
    if field == "gross_weight_kg":
        return extract_number(value)
    return " ".join(str(value).casefold().split())


def extract_all_fields_detailed(text):
    """
    Returns (fields, blanks):
      fields: {field: value or None}  (numbers parsed for the 2 numeric fields)
      blanks: set of fields the document shows but leaves blank / placeholder
    """
    matches = {f: [] for f in FIELD_NAMES}
    blanks = set()
    lines = [l.strip() for l in str(text or "").split("\n")]

    for i, line in enumerate(lines):
        if not line:
            continue
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        field, raw = _match_line(line, next_line)
        if field is None:
            continue
        if raw is None or raw == "" or is_placeholder(_clean_value(raw)):
            blanks.add(field)
            continue
        matches[field].append(_clean_value(raw))

    fields = {}
    for field in FIELD_NAMES:
        values = matches[field]
        if field == "container_count":
            parsed = [extract_container_count(v) for v in values]
        elif field == "gross_weight_kg":
            parsed = [extract_number(v) for v in values]
        else:
            parsed = values
        parsed = [p for p in parsed if p is not None]

        unique = {}
        for p in parsed:
            unique.setdefault(_dedupe_key(field, p), p)
        if len(unique) == 1:
            fields[field] = next(iter(unique.values()))
            blanks.discard(field)          # a real value elsewhere beats a blank
        else:
            fields[field] = None           # not found, or conflicting -> AI decides
            if len(unique) > 1:
                blanks.discard(field)
    return fields, blanks


def extract_all_fields(text):
    """Same return shape as before: {field: value or None}."""
    fields, _ = extract_all_fields_detailed(text)
    return fields
