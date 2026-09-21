# SDOC — Shipping Document Verification

Averis x Monash Hackathon 2026. From an email inbox to a discrepancy report:

1. **Classify** every email: `BL_COMPARISON`, `SI_REQUEST`, `INVOICE_QUERY`, `GENERAL`, `SPAM`
2. **Extract** the 7 fields from the Shipping Instruction (SI) and draft Bill of Lading (BL):
   txt, PDF, Word, Excel, and scanned documents (OCR)
3. **Compare** them and flag mismatches, showing SI and BL values side by side
4. **Ask for help**: anything the system can't decide goes to a human review queue,
   with the reason and the source evidence; the reviewer's decision updates the report

**Using the website:** see [docs/USER_GUIDE.md](docs/USER_GUIDE.md) (pages, input formats,
what the results mean, deleting, how it works).

**Live site:** see [DEPLOY.md](DEPLOY.md). It deploys the UI + API as one container
(Hugging Face Spaces, free), which is the link the judges use.

---

## Quick start

Needs **Python 3.10+**. Node.js 20+ is only needed for the web UI.

```bash
# 1. (recommended) a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux

# 2. set everything up (creates .env, installs packages, builds the UI)
python setup_project.py

# 3. put your AI key in .env        LLM_API_KEY=gsk_...   (console.groq.com)
#    and tell it where the data is  SDOC_DATA=data_v2     (see "Data" below)

# 4. check, run, score
python -m sdoc check
python -m sdoc run
python -m sdoc score
```

No AI key? It still runs: `python -m sdoc run --no-ai` uses the rule-based
parser and OCR only.

## Data

Two ways, exactly as in the use-case document. Set `SDOC_DATA` in `.env`:

| Option | `.env` | Scoring |
|---|---|---|
| **Static bundle**: put the extracted data folder (with `inbox/` and `attachments/`) in the project root | `SDOC_DATA=data_v2` | `python -m sdoc score` if the folder has a `ground_truth.json` |
| **Local server**: `docker compose up --build` in the organizers' package | `SDOC_DATA=http://localhost:8080` | `python -m sdoc run --submit` (their `POST /submit` scoreboard) |

The data folder is git-ignored: never commit it (the organizer package
contains the answer key).

## Commands

```bash
python -m sdoc check                  # what's set up, what's missing (and how to fix it)
python -m sdoc run                    # process the inbox -> output/
python -m sdoc run --no-ai            #   ...without AI calls
python -m sdoc run --retry            #   ...re-run only emails that failed (AI quota, network)
python -m sdoc run --submit           #   ...and score on the organizers' server
python -m sdoc score                  # score output/submission.json locally
python -m sdoc review list            # the human review queue, from the terminal
python -m sdoc review correct email_516 --si gross_weight_kg=230000 --by Ali
python -m sdoc serve                  # web UI + API -> http://127.0.0.1:8000
```

Outputs in `output/`: `submission.json` (the self-evaluation format),
`report.md` (readable report), `review_queue.json`, `results.json` (full detail),
`final_submission.json` (with reviewers' decisions applied).

## Web UI

After `python setup_project.py` the UI is built, so `python -m sdoc serve`
serves everything on one address. Click **Run pipeline** on the dashboard.

Pages: **Dashboard**, **Inbox**, the **SI ↔ BL comparison**, **Human Review**, and
**Upload & Check**:
- **Whole inbox**: upload a `.zip` of an inbox (same layout as the dataset), email `.json`
  files, or saved `.eml` emails with their attachments. It's processed in the background, and
  you can download a `submission.json` keyed by your own email ids. There's a sample inbox to
  download if you have no files.
- **Single email**: paste an email and/or upload an SI and a BL (txt, PDF, Word, Excel or a
  scanned image) and see the result immediately.

To work on the UI with hot reload, run both:

```bash
python -m sdoc serve                  # terminal 1 - API on :8000
cd frontend && npm run dev            # terminal 2 - UI on http://localhost:5173
```

## Configuration

Everything is in **`.env`** (template: [`.env.example`](.env.example), every
option is explained there). The main ones:

| Variable | What it does |
|---|---|
| `LLM_API_KEY` | AI key (Groq by default). Empty = no AI |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_VISION_MODEL` | switch AI provider/model (any OpenAI-compatible API) |
| `SDOC_DATA` | data folder or organizers' server URL |
| `SDOC_OUT` | output folder (default `output`) |
| `SDOC_EXTRACTION_MODE` | `ai` (default) / `auto` / `rules` |
| `SDOC_OCR_POLICY` | `review` (scans go to a human, default) / `trust` |
| `SDOC_API_PORT` | web port (default 8000) |

Real environment variables override `.env` (that's how the cloud deploy will
inject secrets).

## How it meets the brief

| Requirement | Where |
|---|---|
| Classify 5 categories | `sdoc/classify.py`: rules first, AI for ambiguous emails |
| Extract 7 fields, labels differ between SI and BL | `sdoc/label_matching.py`, `sdoc/document_reader.py` |
| Compare, SI vs BL side by side, "No mismatch detected." | `sdoc/compare.py`, report + UI |
| Human in the loop with context | `sdoc/review.py`, UI Review page |
| PDF & Word attachments, tables, layouts | `sdoc/format_readers.py` + AI document analysis |
| Scanned documents (OCR / vision) | `sdoc/ocr.py`, vision fallback in `sdoc/ai_extraction.py` |
| Messier inputs: real discrepancy vs reading/formatting issue | formatting-insensitive exact comparison; OCR values never auto-trusted |
| Unreadable / missing value → review with evidence; confirm or correct; update report | `review.py`, `final_submission.json`, `report.md` |
| Failures visible, retries | per-email retryable flag, `run --retry`, on-disk cache |

## Project layout

```
sdoc/                  the Python package
  config.py            every setting (reads .env)
  llm.py               the one AI client
  loader.py            organizers' data loader (unchanged)
  classify.py          stage 1 - email category
  email_utils.py       SI/BL attachment detection, email-body cleaning
  format_readers.py    PDF / DOCX / XLSX -> text
  ocr.py               scanned pages -> text
  label_matching.py    rule-based field extraction
  ai_extraction.py     AI document analysis (text + vision)
  document_reader.py   any attachment -> one JSON document record
  extraction.py        stage 2 - read the SI + BL of an email
  compare.py           stage 3 - compare the 7 fields
  review.py            human review decisions + report
  pipeline.py          runs everything over the inbox
  scoring.py           local scoring
  uploads.py           Upload & Check storage + deleting (records, files, decisions)
  batch.py             whole-inbox uploads: zip / .json / .eml parsing, freestyle records
  api.py               web API for the UI
  __main__.py          the `python -m sdoc ...` commands
frontend/              React UI
tests/                 python tests/test_classify.py, python tests/test_documents.py
docs/                  use-case PDF, organizers' README + docker-compose
deploy/deploy_hf.py    one-command deploy to Hugging Face Spaces
Dockerfile             the production container (UI + API)
setup_project.py       one-command setup
```

## Tests

```bash
python tests/test_classify.py
python tests/test_documents.py
```

No API key needed: the AI is replaced by a fake client in tests.

## Troubleshooting

- **`python -m sdoc check` says data not reachable**: set `SDOC_DATA` in `.env`. A relative
  path is relative to this project folder.
- **Docker server shows `"emails": 0`**: move the organizers' package under your home folder
  (Docker Desktop can't share `/tmp`), then `docker compose up --build` again.
- **Port 8000 or 8080 already in use**: change `SDOC_API_PORT` in `.env`, or the host port in
  the organizers' `docker-compose.yml`.
- **Red squiggles in VS Code in `frontend/`**: run `npm install` in `frontend/`, then
  *TypeScript: Restart TS Server*.
>>>>>>> cc5de92 ( file)
