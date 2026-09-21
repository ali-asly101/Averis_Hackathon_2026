# CargoSense — User Guide

CargoSense reads a shipping operations inbox, sorts every email, and for
document-check requests compares the **Shipping Instruction (SI)** with the
**draft Bill of Lading (BL)**, flagging exactly which of the 7 fields differ.
Anything it can't decide safely goes to a person, with the reason and the evidence.


---

## 1. The pages

On your first visit, the dashboard shows a short **tour** (dismiss it with *Got it*). The
site also works on phones and tablets.

### Dashboard
The overview of the provided inbox (520 emails): how many were processed, how many
document checks found a **discrepancy**, how many wait for **human review**, and the
**automation rate** (share decided without a person). *Document Checks* lists the
comparison requests that need attention first; click one to open it.

Once you've uploaded anything through Upload & Check, a **Your uploads** panel appears
underneath with its own numbers (emails, document checks, mismatches, cases needing
review), your latest uploads, and shortcuts to review them or see them in the Inbox. Your
uploads are kept **separate** from the main numbers on purpose: those describe the
provided inbox and match the official submission.

### Inbox
Every email with its **category**, **status**, how it was verified, and its attachment
types. Search by subject, sender or email id; filter by category (each filter shows how
many emails it holds), or by **Uploaded** to see only what you added.

**Click any email** to open it. Every email page says, in plain words, *why* it was put in
its category (for example "An SI and a BL are attached and the email asks for them to be
checked") and whether rules or the AI decided it, and shows what the sender wrote. Document
comparison requests also show the SI ↔ BL comparison below.

### SI ↔ BL comparison
The 7 fields side by side: **shipper, consignee, notify party, port of loading, port of
discharge, container count, gross weight (kg)**.

- **No mismatch detected.** (green) means all seven agree; **Nothing to compare yet** means
  the email asked for the draft BL to be sent and no documents came with it.
- A mismatch is highlighted, e.g. *Container Count, SI: 3 / BL: 4*. Only the fields that
  really differ are flagged.
- Under each value you can see how it was read (*Rule parser*, *Rules + AI agree*, *OCR*).
  Hover a value to see the exact text it was read from.
- *Open …* buttons show the original attachments.

### Human Review
Cases the system would not decide on its own. For each case you see **why it was
escalated**, the **source evidence** (the text read from each document, OCR confidence,
warnings), and the SI/BL values it could read. You can:

| Action | When to use it |
|---|---|
| **Accept the system's result** | only the category was uncertain (e.g. the AI was unavailable) and the result is right |
| **Confirm proposal** | the system's proposed result (e.g. from an OCR-read scan) is right |
| **Save values & re-check** | fix or fill in values; the 7 fields are compared again |
| **Acknowledge — request new documents** | wrong or missing document; nothing to compare yet |
| **Reclassify email as…** | the email was put in the wrong category |

The decision updates the dashboard, the report and the final results immediately, and
is recorded with the reviewer's name. When you've uploaded emails, the queue is split into
**Provided inbox** and **Your uploads**, and uploaded cases show your own email ids.

### Upload & Check
Try it with your own emails and documents. Two tabs:

**Whole inbox** — upload many emails at once (see *Input formats* below). They're
processed in the background with a progress bar. Then you get:
- a results table (click a row for its comparison),
- **Download submission.json**: the results in the official self-evaluation format,
  keyed by *your* email ids, ready to score against your own answer key,
- **Delete this batch**.

No files at hand? Click **Download a sample inbox (20 emails)**, then upload that file.

**Single email** — paste an email and/or attach one SI and one BL. The result appears
right away. Leave the text empty and just attach the two documents to go straight to the
comparison.

### Live status — you always know what's happening
Checking a scan or waiting on the AI can take a little while, so the site always shows
what it's doing:

- **While a check or batch runs**, a live panel shows the current step ("Reading the SI
  (PDF)", "Scanned document - reading the BL with OCR", "The AI is reading the SI",
  "Comparing the 7 fields"), the elapsed time and, for batches, *email X of Y* with the
  email being worked on.
- **The coloured label says who's working:**

  | Label | Meaning |
  |---|---|
  | Local rules | fast rule-based work: classifying, reading text / PDF / Word / Excel |
  | OCR scanning | reading a scanned page (a few seconds per page) |
  | AI working | waiting for the AI model's answer |
  | AI waiting | the AI provider asked us to slow down; a countdown shows when it retries |
  | Comparing | comparing the 7 fields |

- **The status light in the sidebar** (every page) shows whether the system is idle or
  working, and the AI's state: ready, working, rate-limited (with the countdown), off, or
  "had trouble recently". If the AI can't answer, processing carries on without it and
  anything it can't decide safely goes to Human Review. Nothing gets stuck.

### Moving around while things run
You never have to wait on a page. Start an inbox upload or a single check, then go
anywhere:

- A small **task card** appears in the bottom-right corner of every other page, showing
  progress and what's happening. Click it to jump back to the results. When the task
  finishes, the card turns into a short summary ("All done: 20 emails checked · 3
  mismatches") until you open or dismiss it.
- **Nothing is lost between pages:** results, the *Download submission.json* button, a
  half-typed email, the files you picked, and which tab you were on are all still there
  when you come back. This even survives **reloading the page**.
- Every uploaded batch stays downloadable later from **Dashboard → Your uploads**.
- **If the server restarts** mid-task, the card says so plainly: what was finished (those
  results are kept), what wasn't, and to upload the rest again. It never just goes blank.
- Your **reviewer name** is remembered, so you only type it once.

---

## 2. Input formats

### Emails (Whole inbox tab)
Any mix of:

| Upload | What it is |
|---|---|
| `.zip` | an inbox in the dataset layout: `inbox/*.json` + `attachments/` (any folder name) |
| `.json` | one email, a list of emails, or `{"emails": [...]}` |
| `.eml` | a real email saved from Gmail / Outlook / Apple Mail, attachments included |
| attachment files | `.txt .pdf .docx .xlsx .png .jpg .jpeg .tif .tiff .bmp .webp` |

**An email record, as in the dataset:**
```json
{
  "email_id": "my_test_01",
  "from": "ops@forwarder.com",
  "subject": "TO CONFIRM DOCS _ 5RSG-51584",
  "body": "Hi, attached are the SI and draft BL. Please check the details and confirm.",
  "attachments": ["attachments/my_test_01_SI.pdf", "attachments/my_test_01_BL.pdf"]
}
```

Improvised variations are accepted too: keys in any case, `sender` for `from`,
`title` for `subject`, `text` / `content` / `message` for `body`, `files` for
`attachments`, the body as a list of lines, attachments as `{"filename": ...}` objects
or a single string, and a missing `email_id` (the file name is used).

### Attachments
- They're matched to emails **by file name**, so upload the files the emails refer to.
  Folder paths don't matter (`attachments/x_SI.pdf` matches an uploaded `x_SI.pdf`).
- **Name the SI and BL files with `SI` and `BL` as separate parts of the name**, like the
  dataset: `anything_SI.pdf`, `anything_BL.docx`, `SI-booking123.xlsx`. That is how the
  system knows which document is which (`contract_SIGNED.pdf` is *not* an SI).
- A file an email refers to but that wasn't uploaded makes that email
  **Needs review — attachment missing** (you'll also see a note after uploading).
- Scanned documents (image-only PDFs, photos) are read with OCR. Word and Excel tables
  and different page layouts are supported.

### Limits
10 MB per file, 60 MB and 1,000 emails per batch, one batch processed at a time, and a
few uploads per visitor per hour (they use the AI).

---

## 3. What the results mean

| Category | Meaning |
|---|---|
| **BL Comparison** | a request to check an SI against a draft BL → compared |
| SI Request | a new shipping instruction → classified only |
| Invoice Query | billing / charges question → classified only |
| General | updates, notices, reminders → classified only |
| Spam | junk, scams, phishing → classified only |

For **BL Comparison** emails:

| Status | Meaning |
|---|---|
| **Verified** (OK) | all 7 fields match, or it's a "please send the draft BL" request with nothing to compare yet |
| **Mismatch** | one or more fields differ; the fields are listed |
| **Needs Review** | the system can't decide safely (reason below) |

| Review reason | Typical cause |
|---|---|
| Wrong document attached | e.g. a commercial invoice or packing list where the BL should be |
| Attachment missing | the SI or BL isn't there |
| Document unreadable / scanned | corrupt file, empty file, or a scan (OCR values are proposed, not trusted) |
| Required value missing | a field is blank / `N/A` / `???` / `TBA` in a document |

A blank or an unreadable document is never reported as a mismatch: that would be a false
alarm. It goes to a person instead.

---

## 4. Deleting

- **Your uploads can be deleted**: the ✕ on an uploaded row in the Inbox, *Delete email* /
  *Delete batch* on its comparison page, *Delete* on a single-email result, *Delete this
  batch* after a batch, or **Delete all uploads** in the Inbox toolbar.
- Deleting removes **everything linked**: the email, its stored attachment files (shared
  files are kept while another email still uses them), its review decision, and the batch
  folder once it's empty.
- **Emails of the provided dataset can't be deleted**. The official submission must
  include every one of them. To undo a reviewer decision on them, reopen the case instead.

---

## 5. How it works

```
email ─► 1. Classify ─► BL Comparison? ─► 2. Read SI + BL ─► 3. Extract 7 fields ─► 4. Compare ─► result
              │                                                                          │
              └─ other categories: classified only                     can't decide ─► 5. Human review
```

1. **Classify.** Clear cases are decided by rules that look at the sender's own text (not
   forwarded threads or "external sender" banners), the attachments and the subject. Only
   genuinely ambiguous emails are sent to the AI.
2. **Read the documents.** Text files are read directly; PDFs from their text layer (keeping
   labels and values apart even in tight layouts); Word and Excel including tables; scanned
   pages with OCR, falling back to a vision AI model when OCR can't read them. The system also
   checks the document is really an SI / a BL.
3. **Extract the 7 fields.** A rule parser that knows the different labels used for the same
   field ("Port of Loading", "Load Port", "POL"…) and an AI reader both read each document. When
   they agree, the value is marked *Rules + AI agree*. An AI value is only accepted if it can be
   found in the document, so invented values are rejected. Placeholders like `N/A` count as blank.
4. **Compare.** Formatting differences aren't discrepancies: case, spacing, punctuation,
   `131,058 KG` vs `131058`, `NANTONG, CHINA (CNNTG)` vs `CNNTG`. Real differences are:
   another company, another port (even if the port code was left unchanged), another number.
   There is no fuzzy "close enough" matching of names.
5. **Human review.** Anything uncertain (scans, blanks, wrong or missing documents, AI outages)
   is escalated with the reason and the evidence, never guessed and never silently dropped.

**Reliability:** processing failures (e.g. the AI service is busy) are shown and can be retried;
results of OCR and AI are cached so repeat runs don't redo work.

---

## 6. Good to know

- The site runs in a container that restarts on every redeploy (and after long idle
  periods); uploads and reviewer decisions from before a restart are then gone. The provided
  inbox is always there.
- Everything you upload is visible to other visitors of the site. Don't upload confidential
  documents.
- The first request after a quiet period can take a few seconds while the server wakes up.
