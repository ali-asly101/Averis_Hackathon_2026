// api.ts — typed client for the Python API (api.py) + mapping from backend
// values to the labels the UI shows.
//
// Dev:  `uvicorn api:app --port 8000` + `npm run dev` (vite proxies /api -> :8000)
// Prod: the Python server serves the built frontend, so same-origin "/api".
// Override with VITE_API_URL (e.g. a separate cloud backend URL).

const BASE = (import.meta.env.VITE_API_URL ?? "").replace(/\/$/, "");

export type BackendCategory =
  | "BL_COMPARISON"
  | "SI_REQUEST"
  | "INVOICE_QUERY"
  | "GENERAL"
  | "SPAM";
export type BackendStatus = "OK" | "MISMATCH" | "NEEDS_REVIEW";
export type ReviewReason =
  | "wrong_doc_type"
  | "missing_attachment"
  | "unreadable"
  | "missing_value"
  | null;

export type Category =
  | "BL Comparison"
  | "SI Request"
  | "Invoice Query"
  | "General"
  | "Spam";
export type Status = "Verified" | "Mismatch" | "Needs Review" | "Classified";

export type Basis = {
  kind: "ocr" | "cross_checked" | "ai" | "rules" | "error";
  label: string;
  score: number | null;
};

export type EmailRow = {
  id: string;
  subject: string;
  sender: string;
  attachments: number;
  attachment_types: string[];
  category: BackendCategory;
  status: BackendStatus;
  review_reason: ReviewReason;
  defect_fields: string[];
  needs_review: boolean;
  reviewed: boolean;
  retryable: boolean;
  basis: Basis;
  summary: string | null;
  source: "dataset" | "upload";
  batch_id?: string | null;
  original_id?: string | null;
};

export type DeleteResult = {
  deleted: { emails: number; files: number; decisions: number; ids: string[] };
};

export type FieldRow = {
  field: string;
  label: string;
  si: string | number | null;
  bl: string | number | null;
  match: boolean | null;
  method: string | null;
  similarity: number | null;
  si_source: string | null;
  bl_source: string | null;
  si_evidence: string | null;
  bl_evidence: string | null;
  readers_disagreed: Record<string, unknown> | null;
};

export type DocumentInfo = {
  side: "SI" | "BL";
  path: string | null;
  file_type: string | null;
  read_method: string | null;
  document_type: string | null;
  ocr: { engine: string; confidence: number } | null;
  warnings: string[];
  error: string | null;
  text_preview: string;
};

export type EmailDetail = EmailRow & {
  classification: { category: string; method: string; review_reason: string | null } | null;
  body_preview?: string | null;
  note: string | null;
  proposed_result: { status: string; defect_fields: string[]; missing_fields?: string[] } | null;
  view: { source: string | null; fields: FieldRow[] };
  documents: DocumentInfo[];
  decision: { action: string; reviewer: string | null; at: string; note: string | null } | null;
  error: string | null;
};

export type Summary =
  | { has_results: false }
  | {
      has_results: true;
      emails_processed: number;
      categories: Record<string, number>;
      comparisons: number;
      mismatches: number;
      no_mismatch: number;
      needs_review: number;
      resolved_by_reviewer: number;
      automation_rate: number;
      cross_checked_rate: number | null;
      retryable_failures: number;
      last_run: string | null;
      uploads: UploadSummary;
    };

export type UploadSummary = {
  emails: number;
  batches: number;
  categories: Record<string, number>;
  comparisons: number;
  mismatches: number;
  no_mismatch: number;
  needs_review: number;
  resolved_by_reviewer: number;
};

export type QueueItem = {
  email_id: string;
  subject: string | null;
  category: BackendCategory;
  status: BackendStatus;
  review_reason: ReviewReason;
  why: string | null;
  proposed: string | null;
  retryable: boolean;
  original_id?: string | null;
  source?: "dataset" | "upload";
};

export type RunStatus = {
  running: boolean;
  done: number;
  total: number;
  started: string | null;
  finished: string | null;
  error: string | null;
  mode: string | null;
  summary: { emails: number; retryable_failures: number } | null;
  activity?: Activity | null;
  // minimum gap between full runs (server setting) and seconds left before the next one;
  // admin_bypass: whether the server accepts an admin token to skip it at all.
  // Optional so an older backend without it still works (no cooldown / no bypass shown)
  cooldown?: { seconds: number; remaining: number; admin_bypass?: boolean } | null;
};

export type Decision = {
  action: "confirm" | "correct" | "override" | "reopen";
  reviewer?: string;
  note?: string;
  si_fields?: Record<string, string>;
  bl_fields?: Record<string, string>;
  category?: BackendCategory;
  status?: BackendStatus;
  defect_fields?: string[];
  review_reason?: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  return parse<T>(res);
}

async function parse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) message = String(body.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, message);
  }
  return res.json() as Promise<T>;
}

export type BatchRow = EmailRow;

export type BatchSummary = {
  id: string;
  running: boolean;
  done: number;
  total: number;
  started: string | null;
  finished: string | null;
  error: string | null;
  emails: number;
  mismatches: number;
  needs_review: number;
  ok: number;
};

export type ActivityMode = "local" | "ocr" | "ai" | "ai_wait" | "compare";

export type Activity = {
  job_id: string;
  kind: "run" | "batch" | "check";
  label: string | null;
  target?: string | null;
  step: string;
  detail: string | null;
  mode: ActivityMode;
  email: string | null;
  done: number;
  total: number | null;
  elapsed: number;
  running: boolean;
  error: string | null;
};

export type ActivitySnapshot = {
  ai: {
    enabled: boolean;
    reason_off: string | null;
    model: string;
    in_flight: number;
    waiting_seconds: number;
    calls: number;
    recent_error: string | null;
  };
  jobs: Activity[];
  job?: Activity | null;
};

export type BatchStatus = {
  id: string;
  running: boolean;
  done: number;
  total: number;
  attachments?: number;
  warnings: string[];
  error: string | null;
  counts?: Record<string, number>;
  rows?: BatchRow[];
  activity?: Activity | null;
};

// Admin token for starting full runs on a protected deploy (asked once, kept for the tab)
const TOKEN_KEY = "sdoc_admin_token";
// (storage can be blocked, e.g. some private modes: then there's simply no saved token)
export const adminToken = {
  get: () => {
    try {
      return sessionStorage.getItem(TOKEN_KEY) ?? "";
    } catch {
      return "";
    }
  },
  set: (t: string) => {
    try {
      sessionStorage.setItem(TOKEN_KEY, t);
    } catch {
      /* not kept: the token will be asked for again next time */
    }
  },
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export const api = {
  summary: () => request<Summary>("/api/summary"),
  emails: () => request<EmailRow[]>("/api/emails"),
  email: (id: string) => request<EmailDetail>(`/api/emails/${encodeURIComponent(id)}`),
  queue: () => request<QueueItem[]>("/api/review"),
  decide: (id: string, decision: Decision) =>
    request<EmailDetail>(`/api/review/${encodeURIComponent(id)}`, {
      method: "POST",
      body: JSON.stringify(decision),
    }),
  runStatus: () => request<RunStatus>("/api/run"),
  startRun: (opts: { retry?: boolean; no_ai?: boolean } = {}) =>
    request<RunStatus>("/api/run", {
      method: "POST",
      body: JSON.stringify(opts),
      headers: { "Content-Type": "application/json", "X-Admin-Token": adminToken.get() },
    }),
  // Upload & Check, many emails: zip / .json / .eml + attachment files
  startBatch: async (files: File[]) => {
    const form = new FormData();
    files.forEach((f) => form.append("files", f));
    return parse<BatchStatus>(await fetch(`${BASE}/api/batch`, { method: "POST", body: form }));
  },
  batch: (id: string, rows = true) =>
    request<BatchStatus>(`/api/batch/${encodeURIComponent(id)}${rows ? "" : "?rows=false"}`),
  batches: () => request<BatchSummary[]>("/api/batches"),
  batchSubmissionUrl: (id: string) => `${BASE}/api/batch/${encodeURIComponent(id)}/submission`,
  sampleBundleUrl: () => `${BASE}/api/sample-bundle`,
  activity: (job?: string) =>
    request<ActivitySnapshot>(`/api/activity${job ? `?job=${encodeURIComponent(job)}` : ""}`),
  // Deleting uploads (record + attachment files + review decision)
  deleteEmail: (id: string) =>
    request<DeleteResult>(`/api/emails/${encodeURIComponent(id)}`, { method: "DELETE" }),
  deleteBatch: (id: string) =>
    request<DeleteResult>(`/api/batch/${encodeURIComponent(id)}`, { method: "DELETE" }),
  deleteAllUploads: () =>
    request<DeleteResult>("/api/uploads", {
      method: "DELETE",
      headers: { "Content-Type": "application/json", "X-Admin-Token": adminToken.get() },
    }),
  // Upload & Check: multipart form (subject, sender, body, si, bl)
  // include a "job_id" form field to follow the check live via activity(job_id)
  check: async (form: FormData) =>
    parse<EmailDetail>(await fetch(`${BASE}/api/check`, { method: "POST", body: form })),
  attachmentUrl: (id: string, index: number) =>
    `${BASE}/api/emails/${encodeURIComponent(id)}/attachments/${index}`,
};

// ---------------------------------------------------------------- display mapping
export const CATEGORY_LABEL: Record<BackendCategory, Category> = {
  BL_COMPARISON: "BL Comparison",
  SI_REQUEST: "SI Request",
  INVOICE_QUERY: "Invoice Query",
  GENERAL: "General",
  SPAM: "Spam",
};

export function statusLabel(category: BackendCategory, status: BackendStatus): Status {
  if (category !== "BL_COMPARISON") return "Classified";
  if (status === "OK") return "Verified";
  if (status === "MISMATCH") return "Mismatch";
  return "Needs Review";
}

export const REVIEW_REASON_LABEL: Record<string, string> = {
  wrong_doc_type: "Wrong document attached",
  missing_attachment: "Attachment missing",
  unreadable: "Document unreadable / scanned",
  missing_value: "Required value missing",
};

export const SOURCE_LABEL: Record<string, string> = {
  "rules+ai": "Rules + AI agree",
  rules: "Rule parser",
  ai: "AI",
  ocr: "OCR",
  vision: "Vision AI",
};

// Why the classifier chose a category, in plain words (method -> explanation)
const METHOD_EXPLANATION: Record<string, string> = {
  spam_rules: "It matches known spam / phishing patterns (sender or scam phrases).",
  "si_bl_attached+comparison_wording": "An SI and a BL are attached and the email asks for them to be checked.",
  "si_bl_attached+unrecognized_wording": "An SI and a BL are attached, but the wording was unusual.",
  "partial_si_bl+comparison_wording": "It asks for a check, but only one of the SI / BL is attached.",
  "si_attached+si_request_wording": "An SI is attached and the email presents a new shipping instruction.",
  "si_attached+unrecognized_wording": "An SI is attached, but the wording was unusual.",
  "bl_attached+unrecognized_wording": "A BL is attached, but the wording was unusual.",
  explicit_bl_check_request: "It explicitly asks for the draft BL to be sent or checked against the SI.",
  si_request_wording: "It presents or requests a new shipping instruction.",
  invoice_wording: "It asks about an invoice, charges or billing.",
  "spam_signals_vs_business_evidence": "It had both spam signals and real business content.",
  weak_spam_signal_only: "It had a weak spam signal and nothing else.",
  no_signal_default: "No document request, billing or spam signals: an operational / general message.",
  malformed_email_record: "The email record was malformed.",
};

export function explainClassification(method: string | undefined | null): string {
  if (!method) return "";
  const base = method.replace(/->ai(_failed_fallback)?$/, "");
  const why = METHOD_EXPLANATION[base] ?? "";
  if (method.endsWith("->ai_failed_fallback"))
    return `${why} The AI couldn't be reached, so this is the best guess from the attachments - a person should confirm it.`.trim();
  if (method.endsWith("->ai")) return `${why} The AI read the email and decided the category.`.trim();
  return `${why} Decided by rules (no AI needed).`.trim();
}

export function fieldLabel(field: string): string {
  return field.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function formatValue(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number") return v.toLocaleString();
  return v;
}
