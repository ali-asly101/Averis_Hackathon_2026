# Deploying the live site

The judges test through a public link. The whole app (UI + API) runs as **one
container**, so there is one thing to deploy and one link to submit.

## Recommended: Hugging Face Spaces (free, no credit card)

Free CPU Spaces have plenty of RAM for the OCR models, give you a public HTTPS
URL, and build straight from the included `Dockerfile`.

### 1. One-time setup (~5 min)

1. Create an account at https://huggingface.co
2. Make a **write** token: https://huggingface.co/settings/tokens → *Create new token* →
   type **Write**
3. In `.env`:
   ```
   HF_TOKEN=hf_xxxxxxxxxxxxxxxx
   HF_SPACE=your-username/cargosense
   LLM_API_KEY=gsk_xxxxxxxxxxxxxxxx      # becomes an encrypted Space secret
   SDOC_DATA=data_v2                     # your local data folder
   ```
4. `pip install huggingface_hub`

### 2. Deploy

```bash
python deploy/deploy_hf.py --dry-run   # builds deploy_build/ and checks it (no upload)
python deploy/deploy_hf.py             # uploads + sets secrets
```

The script:
- copies the code + `data/` (inbox, attachments, sample_submission) into `deploy_build/`
- **never** copies `ground_truth.json` or `.env`, and refuses to upload if one sneaks in
- creates the Space (Docker, public), stores `LLM_API_KEY` as an encrypted **secret**
  (not in any file), and uploads

Then open `https://huggingface.co/spaces/<your-username>/cargosense` to watch the build
(about 5–10 minutes the first time). When it says **Running**, the live site is:

```
https://<your-username>-cargosense.hf.space
```

That's the link for the submission form.

### 3. After deploying: check it like a judge would

- [ ] The site opens with the dashboard already filled in (the inbox is processed while
      the image is built, so there's no wait on startup)
- [ ] Inbox → click a BL comparison → SI and BL side by side
- [ ] Upload & Check → Whole inbox → *Download a sample inbox* → upload it → results +
      *Download submission.json*
- [ ] Upload & Check → Single email → upload an SI and a BL (try a PDF and a scan) → result appears
- [ ] Human Review → resolve a case → the dashboard numbers update
- [ ] Open it in a private/incognito window (no cached login) to confirm it's public

To update the site later, run `python deploy/deploy_hf.py` again.

## Guardrails (already on in the Dockerfile)

Public link = anyone can use it on **your** AI key, so:

| Setting | Default in the container | Effect |
|---|---|---|
| `SDOC_SEED_DIR` | /app/seed | results computed at build time, loaded instantly on startup |
| `SDOC_AUTORUN` | 1 | fallback: processes the inbox on startup if there are no pre-computed results |
| `SDOC_RUN_COOLDOWN` | 300 | a full re-run at most every 5 minutes |
| `SDOC_CHECKS_PER_HOUR` | 30 | Upload & Check requests per visitor IP per hour |
| `SDOC_MAX_UPLOAD_MB` | 10 | max size per uploaded file |
| `SDOC_MAX_BATCH_MB` / `SDOC_MAX_BATCH_EMAILS` | 60 / 1000 | whole-inbox upload limits (one batch runs at a time) |
| `SDOC_BATCHES_PER_HOUR` | 6 | whole-inbox uploads per visitor IP per hour |
| `SDOC_ADMIN_TOKEN` | (off) | set it as a Space secret to make full re-runs need a token |

OCR and AI results are cached, so re-runs after the first cost no AI quota.

## Good to know

- **State resets when the container restarts** (free Spaces sleep after ~48 h without
  visitors and on every redeploy). The inbox is re-processed automatically; reviewer
  decisions and uploads from before the restart are gone. Fine for judging; persistent
  storage is a later step.
- **The Space repository is public** (code + the participant dataset, never the answer
  key). A private Space would not be reachable by the judges.
- **Logs**: the Space page → *Logs* shows the server output (the settings banner,
  AI errors, requests).

## Other hosts

The same `Dockerfile` works anywhere that runs containers. Build it from the
`deploy_build/` folder (it contains `data/`; the main repo doesn't):

```bash
python deploy/deploy_hf.py --dry-run
cd deploy_build
docker build -t cargosense .
docker run -p 7860:7860 -e LLM_API_KEY=gsk_... cargosense     # http://localhost:7860
```

On **Render / Railway / Cloud Run** the platform's `PORT` variable is picked up
automatically; set `LLM_API_KEY` (and optionally `SDOC_ADMIN_TOKEN`) in the
platform's environment/secret settings, never in the image. Note Render's free plan
has 512 MB RAM, which is tight for the OCR engine.
