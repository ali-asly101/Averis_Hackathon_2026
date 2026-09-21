# CargoSense: Shipping Document Verification

**From a shipping inbox to a verified Bill of Lading, automatically.**

Averis x Monash Hackathon 2026 · Team **AIght bet**

| | |
|---|---|
| **Technical documentation** | [TECHNICAL_DOCUMENTATION.md](TECHNICAL_DOCUMENTATION.md) |
| **User guide** | [docs/USER_GUIDE.md](docs/USER_GUIDE.md) |

---

## What it does

A shipping operations inbox mixes document-check requests with new instructions,
invoice questions, updates and spam. For every document-check request, someone has to
compare the **Shipping Instruction (SI)** with the **draft Bill of Lading (BL)** field by
field. CargoSense automates that:

1. **Classify** every email: *BL Comparison*, *SI Request*, *Invoice Query*, *General*, *Spam*.
2. **Extract** 7 fields from the SI and the BL (shipper, consignee, notify party, port of
   loading, port of discharge, container count, gross weight in kg). Plain text, PDF,
   Word, Excel and scanned documents are all supported.
3. **Compare** them and list every mismatch with both values, or report
   **"No mismatch detected."**
4. **Ask for help**: anything it can't decide safely (scans, blanks, wrong or missing
   documents, uncertain categories) goes to a human review queue with the reason and
   evidence. The reviewer's decision updates the report.

## How AI is used

- **Classification:** rules decide the clear cases; an LLM decides ambiguous emails.
- **Extraction:** a label-aware rule parser and an LLM both read every document and
  cross-check each other. LLM values that don't appear in the document are rejected.
- **Scanned documents:** OCR (RapidOCR), with a vision LLM when OCR can't read a page.
- Works with any OpenAI-compatible LLM provider (Groq by default).

## Cloud

The UI and API run as **one Docker container on Hugging Face Spaces**. The provided
inbox is processed while the image builds, so the site opens instantly. API keys are
encrypted platform secrets; the answer key is never deployed.

## Results on the provided sample (520 emails)

| Measure | Result |
|---|---|
| Emails fully correct (category, status, reason, defect fields) | 520 / 520 |
| Edge cases escalated with the correct reason | 20 / 20 |
| False alarms | 0 |

The rules were developed on this sample; unseen wording falls back to the LLM.

---

## Quick start (local)

Requires **Python 3.12** (OCR doesn't install on 3.13+) and **Node.js 18+**.

```bash
python setup_project.py        # creates .venv, installs everything, builds the UI
cp .env.example .env           # then set LLM_API_KEY (Groq: console.groq.com) and SDOC_DATA
python -m sdoc check           # checks your setup and says how to fix anything missing
python -m sdoc serve           # UI + API on http://127.0.0.1:8000
```

The app runs without an AI key too (rules + OCR only).

### Commands

| Command | What it does |
|---|---|
| `python -m sdoc check` | Diagnose the setup |
| `python -m sdoc run` | Process the inbox into `output/` (`--no-ai`, `--retry`, `--submit`) |
| `python -m sdoc score` | Score locally against a ground-truth file |
| `python -m sdoc review list\|show\|confirm\|correct\|override\|reopen` | Human review from the command line |
| `python -m sdoc serve` | Web UI + API |
| `python deploy/deploy_hf.py` | Deploy to Hugging Face Spaces |

### Tests

```bash
python tests/test_classify.py
python tests/test_documents.py
```

### Configuration

All settings live in `.env` (see `.env.example`): the AI provider (`LLM_API_KEY`,
`LLM_BASE_URL`, `LLM_MODEL`, `LLM_VISION_MODEL`), the data source (`SDOC_DATA`: a folder,
or the organizers' server URL), extraction and OCR policies, and web limits.

## Project layout

```
sdoc/            Python package: classify, read documents, OCR, extract, compare,
                 review, uploads, batch parsing, live activity, web API, CLI
frontend/        React + TypeScript UI (Vite)
tests/           test suites
deploy/          one-command Hugging Face deployment
docs/            user guide
Dockerfile       the production container (UI + API)
```

## Team

| Member | Role |
|---|---|
| [Alvaro] | Data extraction |
| [Sachein] | Email classification |
| [Ali] | Comparison, integration, deployment |
| [Elsayed] | Business case | Frontend |
