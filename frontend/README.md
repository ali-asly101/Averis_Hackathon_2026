# Frontend (React + TypeScript + Vite)

The web UI for the SDOC pipeline: dashboard, inbox, SI ↔ BL comparison, and the
human review queue. It talks to the Python API (`python -m sdoc serve`).

```bash
npm install
npm run dev      # http://localhost:5173 - /api is proxied to the Python API
npm run build    # production build in dist/, served by `python -m sdoc serve`
npm run lint
```

Settings come from the project-root `.env` (see `../.env.example`):
`SDOC_API_PORT` (dev proxy target) and optionally `VITE_API_URL` (a separate
backend URL, e.g. in the cloud). Only `VITE_*` values reach the browser.

- `src/api.ts` — typed API client + backend-value → label mapping
- `src/App.tsx` — pages: Dashboard, Inbox, Comparison, Human Review
