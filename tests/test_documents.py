"""
Document reading, AI analysis, OCR/vision, retries and human review.
No API key needed: the AI client is replaced by a fake that answers in the
real format.

  python tests/test_documents.py              # uses SDOC_DATA (default data_v2)
  python tests/test_documents.py path/to/data
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sdoc import config  # noqa: E402

DATA = sys.argv[1] if len(sys.argv) > 1 else config.data_source()
TMP = tempfile.mkdtemp(prefix="sdoc_test_")
os.environ["SDOC_CACHE_DIR"] = os.path.join(TMP, "cache")

from sdoc import label_matching as L  # noqa: E402


# ---------------------------------------------------------------- fake Groq
class Fake:
    mode = "good"
    text_calls = 0
    vision_calls = 0
    last_content = None

    def __init__(self):
        self.chat = self
        self.completions = self

    def create(self, model, messages, **kw):
        content = messages[0]["content"]
        Fake.last_content = content
        if isinstance(content, list):                      # vision request
            Fake.vision_calls += 1
            fields = {f: {"value": v, "status": "found"} for f, v in {
                "shipper": "VISION SHIPPER", "consignee": "C", "notify_party": "C",
                "port_of_loading": "NANTONG, CHINA", "port_of_discharge": "KARACHI, PAKISTAN",
                "container_count": 6, "gross_weight_kg": 131058}.items()}
            return _resp(json.dumps({"document_type": "BILL_OF_LADING", "fields": fields}))
        Fake.text_calls += 1
        if Fake.mode == "fail":
            raise RuntimeError("429 rate_limit exceeded")
        if "<<<EMAIL_START>>>" in content:
            return _resp("BL_COMPARISON")
        doc = content.split("<<<DOCUMENT_START>>>")[1].split("<<<DOCUMENT_END>>>")[0]
        fields, blanks = L.extract_all_fields_detailed(doc)
        if Fake.mode == "hallucinate":
            fields["shipper"] = "TOTALLY MADE UP TRADING LLC"
        if Fake.mode == "disagree" and fields["gross_weight_kg"]:
            # realistic slip: reads a per-container weight (present in the table) as the total
            import re
            per = re.search(r"\b(\d{2},\d{3})\s*$", doc, re.M)
            fields["gross_weight_kg"] = int(per.group(1).replace(",", "")) if per else 1
        out = {f: {"value": v, "status": "blank" if f in blanks else "found"} for f, v in fields.items()}
        return _resp("```json\n" + json.dumps({"document_type": "OTHER", "fields": out}) + "\n```")


def _resp(text):
    msg = type("M", (), {"content": text})()
    return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()


from sdoc import document_reader as R  # noqa: E402
from sdoc import extraction, llm, ocr, pipeline, review  # noqa: E402
from sdoc.loader import Inbox  # noqa: E402

llm.get_client = lambda: Fake()
llm.time.sleep = lambda *a, **k: None
inbox = Inbox(DATA)
passed = 0


def check(name, got, expected):
    global passed
    assert got == expected, f"FAIL {name}: got {got!r}, expected {expected!r}"
    passed += 1
    print(f"ok  {name}")


def fresh_cache():
    shutil.rmtree(os.environ["SDOC_CACHE_DIR"], ignore_errors=True)


def ai_on(mode="good"):
    os.environ.pop("SDOC_DISABLE_AI", None)
    os.environ["LLM_API_KEY"] = "test-key"
    Fake.mode = mode
    fresh_cache()


# ---------------------------------------------------------------- readers
ai_on()
d = R.read_document("attachments/email_059_BL.pdf", inbox, "BL")
check("text PDF read from its text layer", d["read_method"], "pdf_text")
check("PDF fields: rules and AI agree", set(d["field_sources"].values()), {"rules+ai"})
check("PDF recognised as a BL", d["document_type"], "BILL_OF_LADING")

d = R.read_document("attachments/email_055_BL.docx", inbox, "BL")
check("DOCX (bilingual labels) fully read", None in d["fields"].values(), False)
d = R.read_document("attachments/email_055_SI.xlsx", inbox, "SI")
check("XLSX fully read", None in d["fields"].values(), False)

d = R.read_document("attachments/email_512_SI.pdf", inbox, "SI")
check("image-only PDF goes through OCR", d["read_method"], "ocr")
check("OCR confidence recorded", d["ocr"]["confidence"] > 0.9, True)

check("corrupt PDF -> corrupt_file",
      R.read_document("attachments/email_511_BL.pdf", inbox, "BL")["error_code"], "corrupt_file")
check("invoice in the BL slot detected",
      R.read_document("attachments/email_501_BL.txt", inbox, "BL")["document_type"], "COMMERCIAL_INVOICE")

# ---------------------------------------------------------------- AI trust rules
ai_on("hallucinate")
d = R.read_document("attachments/email_059_BL.pdf", inbox, "BL")
check("hallucinated AI value is rejected", d["fields"]["shipper"] != "TOTALLY MADE UP TRADING LLC", True)
check("...and the rejection is visible", any("not found in the document" in w for w in d["warnings"]), True)

ai_on("disagree")
d = R.read_document("attachments/email_059_BL.pdf", inbox, "BL")
check("rules vs AI disagreement recorded", "gross_weight_kg" in d["disagreements"], True)
check("labelled rules value wins on a text document", d["field_sources"]["gross_weight_kg"], "rules")

# ---------------------------------------------------------------- failures + retry
ai_on("fail")
out = os.path.join(TMP, "run")
sub, recs = pipeline.run(DATA, out)
flagged = [r["email_id"] for r in recs if r["retryable"]]
check("AI outage still produces a full submission", len(sub), len(inbox.emails()))
check("AI outage makes emails retryable (visible failure)", len(flagged) > 0, True)

ai_on("good")
before = Fake.text_calls
pipeline.run(DATA, out, retry=True)
recs = json.load(open(os.path.join(out, "results.json")))
check("--retry clears the retryable failures", [r["email_id"] for r in recs if r["retryable"]], [])
check("--retry only re-ran the failed emails", Fake.text_calls - before < len(flagged) * 4, True)

# ---------------------------------------------------------------- vision fallback
ai_on("good")
real_engine = ocr.available_engine
ocr.available_engine = lambda: None                  # simulate: no OCR installed
d = R.read_document("attachments/email_513_BL.pdf", inbox, "BL")
ocr.available_engine = real_engine
check("no OCR engine -> vision model reads the scan", d["read_method"], "vision")
check("vision request carries the page image",
      any(p.get("type") == "image_url" for p in Fake.last_content), True)

os.environ["SDOC_DISABLE_AI"] = "1"
ocr.available_engine = lambda: None
d = R.read_document("attachments/email_513_BL.pdf", inbox, "BL")
ocr.available_engine = real_engine
check("no OCR and no AI -> ocr_unavailable, retryable", (d["error_code"], d["retryable"]),
      ("ocr_unavailable", True))

# ---------------------------------------------------------------- scan policy
os.environ["SDOC_DISABLE_AI"] = "1"
r = extraction.process_email(inbox.get("email_512"), inbox)
check("scan -> escalated for confirmation (unreadable)", r["reason"].startswith("Could not read attachment"), True)
check("scan escalation carries the proposed values", r["si_fields"]["container_count"], 6)

# ---------------------------------------------------------------- human review loop
out = os.path.join(TMP, "review")
pipeline.run(DATA, out)
q = [i["email_id"] for i in review.queue(out)]
check("review queue has the 20 cases", len(q), 20)

case = review.get_case(out, "email_512")["record"]["report"]
if (case.get("proposed_result") or {}).get("status") in ("OK", "MISMATCH"):
    d = review.decide(out, "email_512", "confirm", reviewer="tester")
else:
    try:
        review.decide(out, "email_512", "correct", reviewer="tester")
        check("incomplete correction is refused", False, True)
    except review.ReviewError:
        check("incomplete correction is refused", True, True)
    # the reviewer reads the scan and types in what OCR could not read
    si, bl = case["si_fields"], case["bl_fields"]
    fill = {k: (si.get(k) if si.get(k) is not None else bl.get(k)) for k in si}
    fill.update({k: f"READ BY REVIEWER {k}" for k, v in fill.items() if v is None})
    d = review.decide(out, "email_512", "correct", reviewer="tester", si_fields=fill, bl_fields=fill)
check("reviewer decision resolves the case", d["final"]["status"] in ("OK", "MISMATCH"), True)

d = review.decide(out, "email_516", "correct", reviewer="tester", si_fields={"gross_weight_kg": 999999})
check("correcting a blank value re-runs the comparison", d["final"]["status"], "MISMATCH")
check("...and names the differing field", "gross_weight_kg" in d["final"]["defect_fields"], True)

final = json.load(open(os.path.join(out, "final_submission.json")))
system = json.load(open(os.path.join(out, "submission.json")))
check("final_submission reflects the human decision", final["email_516"]["status"], "MISMATCH")
check("submission.json keeps the system's own answer", system["email_516"]["status"], "NEEDS_REVIEW")
check("resolved cases leave the queue", "email_516" in [i["email_id"] for i in review.queue(out)], False)
check("report.md shows the reviewer", "reviewed by tester" in open(os.path.join(out, "report.md")).read(), True)
check("report says 'No mismatch detected.' for clean emails",
      "No mismatch detected." in open(os.path.join(out, "report.md")).read(), True)

review.decide(out, "email_516", "reopen")
check("reopen puts it back in the queue", "email_516" in [i["email_id"] for i in review.queue(out)], True)

try:
    review.decide(out, "email_511", "confirm")
    check("confirm without a proposal is refused", False, True)
except review.ReviewError:
    check("confirm without a proposal is refused", True, True)

# ---------------------------------------------------------------- Upload & Check
from sdoc import uploads  # noqa: E402
os.environ["SDOC_DISABLE_AI"] = "1"
os.environ["SDOC_OUT"] = os.path.join(TMP, "upload_out")
si = ("mine.pdf", inbox.read_bytes("attachments/email_059_SI.pdf"))
bl = ("theirs.PDF", inbox.read_bytes("attachments/email_059_BL.pdf"))
email = uploads.save_upload(subject="judge", si=si, bl=bl)
check("upload stored under a server-made name", email["attachments"][0].endswith("_SI.pdf"), True)
rec = pipeline.process_one(email, uploads.UploadInbox(inbox))
check("uploaded PDF pair is compared", (rec["submission"]["category"], rec["submission"]["status"]),
      ("BL_COMPARISON", "OK"))
uploads.append_record(rec)
check("upload appears in review records", email["email_id"] in review.load_records(os.environ["SDOC_OUT"]), True)
for bad, why in [((("x.exe", b"MZ"),), "bad extension"), ((), "nothing to check")]:
    try:
        uploads.save_upload(si=bad[0] if bad else None)
        check(f"upload rejected: {why}", False, True)
    except uploads.UploadError:
        check(f"upload rejected: {why}", True, True)
try:
    uploads.UploadInbox(inbox).read_bytes("uploads/../../../etc/passwd")
    check("path traversal blocked", False, True)
except FileNotFoundError:
    check("path traversal blocked", True, True)
# ---------------------------------------------------------------- whole-inbox uploads
import io as _io  # noqa: E402
import zipfile as _zipfile  # noqa: E402
from email.message import EmailMessage  # noqa: E402
from sdoc import batch  # noqa: E402

buf = _io.BytesIO()
with _zipfile.ZipFile(buf, "w") as zf:
    for eid in ("email_004", "email_013"):
        rec = inbox.get(eid)
        zf.writestr(f"x/inbox/{eid}.json", json.dumps(rec))
        for a in rec["attachments"]:
            zf.writestr(f"x/{a}", inbox.read_bytes(a))
    zf.writestr("x/ground_truth.json", "{}")
    zf.writestr("../../evil.json", json.dumps({"email_id": "evil", "body": "hi"}))
p = batch.parse_uploads([("inbox.zip", buf.getvalue())])
check("zip inbox: emails found", sorted(e["email_id"] for e in p.emails), ["email_004", "email_013", "evil"])
check("zip inbox: ground_truth.json ignored", any("ground_truth" in w for w in p.warnings), True)
check("zip without .zip extension still recognised",
      len(batch.parse_uploads([("download", buf.getvalue())]).emails), 3)

m = EmailMessage()
m["From"], m["Subject"] = "ops@x.com", "check docs"
m.set_content("Please check the details and confirm.")
m.add_attachment(inbox.read_bytes("attachments/email_059_SI.pdf"), maintype="application",
                 subtype="pdf", filename="email_059_SI.pdf")
p = batch.parse_uploads([("Mail.eml", m.as_bytes())])
check(".eml parsed with its attachment", (p.emails[0]["subject"], p.emails[0]["attachments"]),
      ("check docs", ["email_059_SI.pdf"]))

os.environ["SDOC_OUT"] = os.path.join(TMP, "batch_out")
p = batch.parse_uploads([("two.json", json.dumps([inbox.get("email_004"), inbox.get("email_013")]).encode()),
                         ("email_004_SI.txt", inbox.read_bytes("attachments/email_004_SI.txt")),
                         ("email_004_BL.txt", inbox.read_bytes("attachments/email_004_BL.txt"))])
check("missing attachments are reported", any("not uploaded" in w for w in p.warnings), True)
bid, emails = uploads.save_batch(p)
recs = {e["original_id"]: pipeline.process_one(e, uploads.UploadInbox(inbox))["submission"] for e in emails}
check("uploaded pair compared like the dataset", recs["email_004"]["status"], "MISMATCH")
check("email with un-uploaded files -> comparison flagged missing_attachment",
      (recs["email_013"]["category"], recs["email_013"]["review_reason"]), ("BL_COMPARISON", "missing_attachment"))
for bad, why in [([("a.pdf", b"%PDF")], "no emails"), ([("x.zip", b"nope")], "broken zip")]:
    try:
        batch.parse_uploads(bad)
        check(f"batch rejected: {why}", False, True)
    except uploads.UploadError:
        check(f"batch rejected: {why}", True, True)

# ---------------------------------------------------------------- freestyle records + deleting
free = batch.normalize_record({"ID": "j 1/x", "Sender": {"email": "a@b.c"}, "Title": "t",
                               "Text": ["line 1", "line 2"], "Files": [{"filename": "x_SI.pdf"}, "x_BL.pdf"]}, "fb")
check("freestyle record normalised to the spec's shape",
      (free["email_id"], free["from"], free["subject"], free["body"], free["attachments"]),
      ("j 1/x", "a@b.c", "t", "line 1\nline 2", ["x_SI.pdf", "x_BL.pdf"]))
check("non-email dict ignored", batch.normalize_record({"foo": 1}, "fb"), None)

out = Path(os.environ["SDOC_OUT"])
up = out / "uploads"
files_on_disk = lambda: sorted(q.name for q in up.rglob("*") if q.is_file()) if up.exists() else []
before = set(files_on_disk())
recs_before = len(uploads.load_records())
p = batch.parse_uploads([("pair.json", json.dumps([inbox.get("email_004"), inbox.get("email_025")]).encode())] +
                        [(f"{e}_{k}.txt", inbox.read_bytes(f"attachments/{e}_{k}.txt"))
                         for e in ("email_004", "email_025") for k in ("SI", "BL")])
bid, emails = uploads.save_batch(p)
uploads.append_records([{**pipeline.process_one(e, uploads.UploadInbox(inbox)), "batch_id": bid,
                         "original_id": e["original_id"]} for e in emails])
check("internal ids are URL-safe", all("/" not in e["email_id"] and " " not in e["email_id"] for e in emails), True)
one = next(e["email_id"] for e in emails if e["original_id"] == "email_004")
r = uploads.delete_email(one)
left = set(files_on_disk()) - before
check("delete one batch email: only its own files go",
      (r["files"], sorted(left)), (2, ["email_025_BL.txt", "email_025_SI.txt"]))
r = uploads.delete_batch(bid)
check("delete batch: the rest + its folder go", (r["emails"], (up / bid).exists()), (1, False))
check("records back to where they were", len(uploads.load_records()), recs_before)
uploads.delete_all()
check("delete all: nothing left", (uploads.load_records(), files_on_disk()), ([], []))
check("deleting something already gone is harmless", uploads.delete_email("nope"), None)
os.environ.pop("SDOC_OUT", None)

# ---------------------------------------------------------------- live status
from sdoc import activity  # noqa: E402
activity.step("nobody is listening")                       # unbound: must be a harmless no-op
activity.start("t1", "check", total=1)
tok = activity.bind("t1")
os.environ["SDOC_DISABLE_AI"] = "1"
R.read_document("attachments/email_512_SI.pdf", inbox, "SI")
check("AI off: no 'AI working' step is shown", activity.job("t1")["mode"] != "ai", True)
activity.step("reading", mode="local")
ai_on("fail")
os.environ["LLM_API_KEY"] = "test-key"
llm.complete([{"role": "user", "content": "x"}], lambda t: t, max_retries=2)
check("rate limit shown as a countdown step", activity.job("t1")["mode"], "local")   # ends: 'continuing without it'
snap = activity.snapshot()
check("AI error recorded for the status light", snap["ai"]["recent_error"], "ai_rate_limited")
activity.unbind(tok)
activity.finish("t1")
check("finished job reported as done", (activity.job("t1")["running"], activity.job("t1")["step"]), (False, "Done"))

# ---------------------------------------------------------------- batches survive restarts
import time as _time  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
os.environ["SDOC_DISABLE_AI"] = "1"
os.environ["SDOC_OUT"] = os.path.join(TMP, "api_out")
from sdoc import api as _api  # noqa: E402
with TestClient(_api.app) as c:
    two = json.dumps([inbox.get("email_004"), inbox.get("email_059")]).encode()
    files = [("files", ("two.json", two, "application/json"))] + [
        ("files", (n, inbox.read_bytes(f"attachments/{n}"), "application/octet-stream"))
        for n in ("email_004_SI.txt", "email_004_BL.txt", "email_059_SI.pdf", "email_059_BL.pdf")]
    bid = c.post("/api/batch", files=files).json()["id"]
    while c.get(f"/api/batch/{bid}?rows=false").json()["running"]:
        _time.sleep(0.2)
    light = c.get(f"/api/batch/{bid}?rows=false").json()
    check("progress polling skips the rows", "rows" not in light or light["rows"] is None, True)
    check("finished batch returns its rows", len(c.get(f"/api/batch/{bid}").json()["rows"]), 2)
    listed = {b["id"]: b for b in c.get("/api/batches").json()}
    check("batch appears in the batch list", bid in listed, True)
    # simulate a restart mid-batch: live state gone, metadata says 5 emails, only 2 were saved
    _api._batches.clear()
    uploads.write_batch_meta(bid, total=5, finished=None)
    st = c.get(f"/api/batch/{bid}").json()
    check("after a restart an unfinished batch is reported as interrupted",
          (st["running"], "Interrupted" in (st["error"] or ""), len(st["rows"])), (False, True, 2))
    q = c.get("/api/review").json()
    check("review queue carries the uploader's own ids", all("original_id" in i for i in q), True)
check("slow emails are recognised (save before them)",
      (_api._slow_email({"attachments": ["a_SI.pdf"]}), _api._slow_email({"attachments": ["a_SI.txt"]})),
      (True, False))
os.environ.pop("SDOC_OUT", None)

print(f"\nALL {passed} CHECKS PASSED")
shutil.rmtree(TMP, ignore_errors=True)
