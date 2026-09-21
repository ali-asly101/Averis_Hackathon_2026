# CargoSense: Technical Documentation

Averis x Monash Hackathon 2026 

Contents: 1. Technical architecture · 2. Implementation details · 3. Challenges faced ·
4. Future roadmap

---

## 1. Technical architecture

### Overview

```
                 ┌──────────── Web UI (React + TypeScript) ────────────┐
                 │ Dashboard · Inbox · Human Review · Upload & Check    │
                 └──────────────────────┬───────────────────────────────┘
                                        │ REST (FastAPI)
┌───────────────────────────────────────▼───────────────────────────────────────┐
│ email → Classifier → Document reader → Field extractor → Comparator → report  │
│          rules+LLM    text/PDF/Word/     rules + LLM,      7 fields            │
│                       Excel/OCR/vision   grounded                   ↘          │
│                                                           Human review queue  │
└───────────────────────────────────────────────────────────────────────────────┘
      LLM provider (Groq, OpenAI-compatible)      OCR (RapidOCR)      disk cache
```

### Components

| Module | Responsibility |
|---|---|
| `classify.py` | Assigns one of 5 categories. Rules over the sender's own text (external-sender banners and forwarded threads removed), attachment names and subject; ambiguous emails go to the LLM |
| `format_readers.py` | Turns PDF (font-aware), Word and Excel into text in reading order, including tables |
| `ocr.py` | Reads scanned pages with RapidOCR, with confidence scores |
| `document_reader.py` | Reads any attachment into a document record: text, how it was read, fields, document type, evidence |
| `label_matching.py` | Rule-based extraction by exact label matching, with ambiguity detection |
| `ai_extraction.py` | LLM document analysis (text and vision), cached on disk |
| `extraction.py` | Reads the SI and BL, detects wrong or missing documents, decides escalation |
| `compare.py` | Compares the 7 fields; returns status, defect fields and a per-field report |
| `review.py` | Human decisions (confirm, correct, override, reopen), audit trail, report |
| `pipeline.py` | Runs everything over an inbox; retries failures |
| `uploads.py`, `batch.py` | Upload & Check: single emails, whole inboxes (zip, JSON, .eml), deletion |
| `activity.py` | Live status of every running task for the UI |
| `api.py` | FastAPI web API and the built UI |

### Deployment

- **One Docker image** (multi-stage: Node builds the UI, Python runs the API and serves
  the UI) on **Hugging Face Spaces**.
- The provided inbox is **processed at image build time** (rules + OCR); results load
  instantly at startup and no AI quota is spent on boot.
- Secrets (`LLM_API_KEY`, optional admin token) are **encrypted Space secrets**, never in
  the image. The deploy script refuses to upload the answer key or `.env`.
- Guardrails for a public link: per-visitor upload rate limits, file size limits, an
  allowlist of file types, server-generated file names, cooldowns on full runs.

---

## 2. Implementation details

### Classification
- Evidence order: spam signals (needing several signals, whole-word matching), then
  attachments (an SI and a BL attached → comparison), then wording.
- Only emails the rules can't decide go to the LLM. If the LLM is unavailable, the best
  guess from the attachments is used and the email is flagged for a person to confirm.

### Reading documents
- **PDF:** a font-aware reader separates bold labels from regular values; without it, the
  text layer interleaves them ("Gross Weightnn(KGS)").
- **Word / Excel:** paragraphs and table cells in reading order.
- **Scans:** image-only pages go through OCR; low confidence falls back to a vision LLM.
- **Wrong documents** (for example a commercial invoice where the BL should be) are
  detected from their content, not their file name.

### Extracting the 7 fields
- The rule parser matches known label variants exactly ("Port of Loading", "Load Port",
  "POL", bilingual labels), after removing noise like "(POL)". The same label with
  conflicting values counts as ambiguous, not as an answer.
- The LLM reads the same document independently. **An LLM value is accepted only if it
  appears in the document text**, which rejects invented values.
- For text documents the labelled source wins when they disagree; for scans the LLM wins.
  Agreement is shown in the UI as *Rules + AI agree*.
- Placeholders (`N/A`, `???`, `TBA`, blank) mean "missing", never a value.

### Comparing
- Formatting-insensitive: case, spacing and punctuation ignored; numbers normalised
  (`131,058 KG` = `131058`); container counts summed (`3 x 20'GP + 4 x 40'HC` = 7).
- **Ports:** UN/LOCODEs are used, but both the place name and the code must agree. A BL
  that keeps the old code while changing the city is a mismatch.
- **No fuzzy matching of names:** a similarity score is recorded as a diagnostic only,
  because a false match would hide a real defect.
- Missing or implausible values (zero or negative weight) go to review, never to a
  mismatch.

### Human review
- Every escalation carries its reason, the text each value was read from, OCR confidence,
  and, for scans, a proposed result that can be confirmed in one click.
- Actions: accept, confirm a proposal, correct values (the comparison re-runs), reclassify,
  acknowledge (request new documents). Decisions are stored with an audit trail and update
  the report and final results.

### Reliability and user experience
- **Live status** of every step (local rules, OCR, AI working, AI rate-limited with a
  countdown). Work runs off the web server's main thread, so one slow check never blocks
  other visitors.
- **Nothing lost between pages:** tasks live at app level and in the browser session, with
  a task dock on every page; a reload reconnects to running work.
- **Restarts are explained:** batch progress is saved as it goes (before each slow email),
  so an interrupted batch keeps what it finished and says so.
- Failures are marked retryable; OCR and LLM results are cached by content hash.

### Testing
- Two suites (100+ checks) cover classification, every document format, OCR, extraction,
  comparison, uploads, deletion, batch recovery and live status.
- End-to-end browser tests drive every page, including tab switching, reloads and server
  restarts mid-task.

---

## 3. Challenges faced

| Challenge | What we did |
|---|---|
| PDF text layers interleave bold labels with values | Font-aware PDF reader that separates labels from values |
| The same field has many labels, some bilingual | Evidence-based label list with exact matching and ambiguity detection |
| A BL kept the old port code but changed the city | Require the place name and the code to agree |
| OCR misreads digits ("6" read as "8") | Scans are proposals for a human, not trusted automatically |
| LLMs can invent plausible values | Accept an LLM value only if it appears in the document |
| LLM rate limits on the free tier | Retries with a visible countdown, caching, results computed at build time |
| OCR needs about 690 MB of memory | Chose hosting with enough memory |
| One slow check froze the web server for everyone | Checks run in worker threads |
| Progress disappeared when changing pages | App-level task state, a task dock, reload and restart recovery |
| An interrupted batch lost finished work | Save results before each slow email |
| Integrating work from several team members | One package with shared settings, tests as the contract between modules |

---

## 4. Future roadmap

**Next:** a database and object storage so uploads and decisions persist; user accounts
and roles; export of the audit trail.

**Then:** live Gmail / Outlook connectors instead of uploads; more document types
(commercial invoices, packing lists); multilingual documents.

**Later:** learning from reviewer corrections; drafting the reply to the sender;
integration with carrier and booking systems.
