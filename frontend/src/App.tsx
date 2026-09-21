import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import "./App.css";
import cargoSenseLogo from "./assets/logo.png";
import { batchSummary, useTasks } from "./tasks";
import type { Task, TaskStore } from "./tasks";
import {
  api,
  adminToken,
  explainClassification,
  ApiError,
  CATEGORY_LABEL,
  REVIEW_REASON_LABEL,
  SOURCE_LABEL,
  formatValue,
  fieldLabel,
  statusLabel,
} from "./api";
import type {
  BatchSummary,
  UploadSummary,
  Activity,
  ActivitySnapshot,
  BackendCategory,
  DeleteResult,
  Category,
  Decision,
  EmailDetail,
  EmailRow,
  FieldRow,
  QueueItem,
  RunStatus,
  Status,
  Summary,
} from "./api";

type Page =
  | "dashboard"
  | "inbox"
  | "comparison"
  | "review"
  | "check";

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// Ask, delete, and report. On a protected deploy, asks for the admin token once.
async function confirmDelete(
  question: string,
  action: () => Promise<DeleteResult>,
): Promise<DeleteResult | null> {
  if (!window.confirm(question)) return null;
  try {
    return await action();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      const token = window.prompt("This deployment needs the admin token for that:");
      if (token) {
        adminToken.set(token);
        try {
          return await action();
        } catch (retryErr) {
          window.alert(errorText(retryErr));
          return null;
        }
      }
      return null;
    }
    window.alert(errorText(err));
    return null;
  }
}

const MODE_LABEL: Record<Activity["mode"], string> = {
  local: "Local rules",
  ocr: "OCR scanning",
  ai: "AI working",
  ai_wait: "AI waiting",
  compare: "Comparing",
};

// "What's happening right now" for one job: mode pill, step, detail, progress.
function LiveStatus({ activity, fallback }: { activity: Activity | null | undefined; fallback?: string }) {
  if (!activity) {
    return fallback ? (
      <div className="live-status">
        <span className="live-spinner" />
        <span className="live-step">{fallback}</span>
      </div>
    ) : null;
  }
  const total = activity.total ?? 0;
  const pct = total ? Math.round((Math.min(activity.done, total) / total) * 100) : 0;
  return (
    <div className={`live-status live-${activity.mode}`}>
      <div className="live-head">
        {activity.running && <span className="live-spinner" />}
        <span className={`mode-pill mode-${activity.mode}`}>{MODE_LABEL[activity.mode]}</span>
        <span className="live-step">{activity.step}</span>
        <span className="live-elapsed">{Math.round(activity.elapsed)}s</span>
      </div>
      {activity.detail && <div className="live-detail">{activity.detail}</div>}
      {total > 1 && (
        <div className="live-progress">
          <div className="progress">
            <div className="progress-bar" style={{ width: `${pct}%` }}></div>
          </div>
          <span>
            {Math.min(activity.done, total)} / {total} emails
            {activity.email ? ` · now: ${activity.email}` : ""}
          </span>
        </div>
      )}
    </div>
  );
}

// Sidebar light: idle / working / AI state, always visible.
// busyLabel: a task this browser started. Shown straight away, so the light
// never says "Idle" in the seconds before the next poll; also polls faster then.
function SystemStatus({ busyLabel }: { busyLabel: string | null }) {
  const [snap, setSnap] = useState<ActivitySnapshot | null>(null);
  const [offline, setOffline] = useState(false);
  const busy = busyLabel !== null;
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const s = await api.activity();
        if (alive) {
          setSnap(s);
          setOffline(false);
        }
      } catch {
        if (alive) setOffline(true);
      }
    };
    tick();
    const timer = setInterval(tick, busy ? 1000 : 2500);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [busy]);

  if (offline) {
    return (
      <div className="system-status">
        <div><span className="status-dot dot-red"></span>Server unreachable</div>
        <small>retrying…</small>
      </div>
    );
  }
  if (!snap) return <div className="system-status"><div><span className="status-dot"></span>Connecting…</div></div>;
  const job = snap.jobs[0];
  const ai = snap.ai;
  const aiLine = !ai.enabled
    ? `AI off (${ai.reason_off})`
    : ai.waiting_seconds > 0
      ? `AI rate-limited · retry in ${ai.waiting_seconds}s`
      : ai.in_flight > 0
        ? `AI working (${ai.model.split("/").pop()})`
        : ai.recent_error
          ? `AI had trouble recently (${ai.recent_error.replace("ai_", "").replace(/_/g, " ")})`
          : `AI ready (${ai.model.split("/").pop()})`;
  return (
    <div className="system-status">
      <div>
        <span className={`status-dot ${job || busyLabel ? "dot-busy" : ""}`}></span>
        <span className="status-line">
          {!job && busyLabel
            ? busyLabel
            : job
            ? job.kind === "batch"
              ? `Checking ${job.label ?? "your upload"}`
              : job.kind === "check"
                ? "Checking one email"
                : job.label ?? "Processing the inbox"
            : "Idle - ready"}
        </span>
      </div>
      {job && job.total && job.total > 1 && (
        <small>{Math.min(job.done, job.total)} / {job.total} emails</small>
      )}
      <small className={ai.enabled ? (ai.waiting_seconds || ai.recent_error ? "ai-warn" : "") : "ai-off"}>
        {aiLine}
      </small>
    </div>
  );
}

function newJobId() {
  const c = globalThis.crypto;
  return c && "randomUUID" in c
    ? c.randomUUID().replace(/-/g, "").slice(0, 24)
    : Math.random().toString(36).slice(2, 14);
}

function deletedMessage(r: DeleteResult) {
  const d = r.deleted;
  return `Deleted ${d.emails} email${d.emails === 1 ? "" : "s"} and ${d.files} file${d.files === 1 ? "" : "s"}` +
    (d.decisions ? ` (and ${d.decisions} review decision${d.decisions === 1 ? "" : "s"})` : "") + ".";
}

const PAGE_LABEL: Record<Page, string> = {
  dashboard: "Dashboard",
  inbox: "Inbox",
  comparison: "Email",
  review: "Human Review",
  check: "Upload & Check",
};

function App() {
  const [page, setPage] = useState<Page>("dashboard");
  const [backTo, setBackTo] = useState<Page>("inbox");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [emails, setEmails] = useState<EmailRow[]>([]);
  const [run, setRun] = useState<RunStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [inboxFilter, setInboxFilter] = useState<InboxFilter>("All");
  // Upload & Check state lives here, so leaving the page never loses it
  const [checkMode, setCheckMode] = useState<"batch" | "single">("batch");
  const [batchFiles, setBatchFiles] = useState<File[]>([]);
  const [singleDraft, setSingleDraft] = useState<SingleDraft>(EMPTY_DRAFT);

  const refresh = useCallback(async () => {
    try {
      const [s, e, r] = await Promise.all([api.summary(), api.emails(), api.runStatus()]);
      setSummary(s);
      setEmails(e);
      setRun(r);
      setError(null);
    } catch (err) {
      setError(`Cannot reach the backend: ${errorText(err)}`);
    }
  }, []);

  const store = useTasks(refresh);
  const { tasks, trackRun, dismiss } = store;

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
  }, [refresh]);

  // a full run that's already going (started elsewhere, or before a reload) shows up as a task
  const runTracked = tasks.some((t) => t.kind === "run" && t.state === "running");
  useEffect(() => {
    if (run?.running && !runTracked) trackRun("Processing the provided inbox");
  }, [run?.running, runTracked, trackRun]);

  // a finished task you're looking at counts as seen: drop it from the dock
  useEffect(() => {
    for (const t of tasks) {
      if (t.dismissed || t.state === "running") continue;
      const visible =
        (page === "check" && checkMode === "batch" && t.kind === "batch" && t.id === store.batchId) ||
        (page === "check" && checkMode === "single" && t.kind === "check") ||
        (page === "dashboard" && t.kind === "run");
      if (visible) dismiss(t.key);
    }
  }, [tasks, page, checkMode, store.batchId, dismiss]);

  const openInbox = (filter: InboxFilter = "All") => {
    setInboxFilter(filter);
    setPage("inbox");
  };

  // sidebar navigation: the Inbox link always opens the full, unfiltered inbox
  const navigate = (p: Page) => {
    if (p === "inbox") setInboxFilter("All");
    setPage(p);
  };

  const startRun = async (opts: { retry?: boolean; no_ai?: boolean }) => {
    try {
      setRun(await api.startRun(opts));
      trackRun(opts.retry ? "Retrying failed emails" : "Processing the provided inbox");
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        const token = window.prompt("This deployment needs the admin token to start a full run:");
        if (token) {
          adminToken.set(token);
          return startRun(opts);
        }
      }
      setError(errorText(err));
    }
  };

  const openEmail = (id: string) => {
    setSelectedId(id);
    if (page !== "comparison") setBackTo(page);
    setPage("comparison");
  };

  const openReview = (id: string | null) => {
    setSelectedId(id);
    setPage("review");
  };

  const openTask = (t: Task) => {
    if (t.kind === "run") return setPage("dashboard");
    if (t.state === "lost") return openInbox("Uploaded");
    if (t.kind === "batch") store.openBatch(t.id);
    setCheckMode(t.kind === "batch" ? "batch" : "single");
    setPage("check");
  };

  const runTask = tasks.find((t) => t.kind === "run" && t.state === "running");

  return (
    <div className="app">
      <Sidebar page={page} setPage={navigate}
               busyLabel={tasks.find((t) => t.state === "running")?.label ?? null} />

      <main className="main-content">
        {error && (
          <div className="api-error">
            {error}
            <button className="secondary-button" onClick={refresh}>
              Retry
            </button>
          </div>
        )}

        {page === "dashboard" && (
          <Dashboard
            summary={summary}
            emails={emails}
            run={run}
            runTask={runTask}
            onRun={startRun}
            onViewInbox={openInbox}
            onOpenEmail={openEmail}
            onOpenReview={() => openReview(null)}
          />
        )}

        {page === "inbox" && (
          <Inbox
            key={inboxFilter}
            emails={emails}
            initialFilter={inboxFilter}
            onOpenEmail={openEmail}
            onChanged={refresh}
          />
        )}

        {page === "comparison" && selectedId && (
          <ComparisonPage
            emailId={selectedId}
            backLabel={PAGE_LABEL[backTo]}
            onBack={() => setPage(backTo)}
            onReview={() => openReview(selectedId)}
            onDeleted={() => {
              refresh();
              setPage(backTo === "check" ? "check" : "inbox");
            }}
          />
        )}

        {page === "review" && <HumanReview initialId={selectedId} onChanged={refresh} />}

        {page === "check" && (
          <CheckPage
            mode={checkMode}
            setMode={setCheckMode}
            store={store}
            batchFiles={batchFiles}
            setBatchFiles={setBatchFiles}
            draft={singleDraft}
            setDraft={setSingleDraft}
            onOpenEmail={openEmail}
            onReview={openReview}
            onChanged={refresh}
          />
        )}
      </main>

      <TaskDock tasks={tasks} page={page} checkMode={checkMode} currentBatch={store.batchId}
                onOpen={openTask} onDismiss={dismiss} />
    </div>
  );
}

// Bottom-right dock: every background task, on every page. Click one to jump back.
function TaskDock({
  tasks,
  page,
  checkMode,
  currentBatch,
  onOpen,
  onDismiss,
}: {
  tasks: Task[];
  page: Page;
  checkMode: "batch" | "single";
  currentBatch: string | null;
  onOpen: (t: Task) => void;
  onDismiss: (key: string) => void;
}) {
  const shownOnPage = (t: Task) =>
    (page === "check" && checkMode === "batch" && t.kind === "batch" && t.id === currentBatch) ||
    (page === "check" && checkMode === "single" && t.kind === "check") ||
    (page === "dashboard" && t.kind === "run");
  const items = tasks.filter((t) => !t.dismissed && !shownOnPage(t)).slice(-3);
  if (!items.length) return null;

  return (
    <div className="task-dock" role="status" aria-live="polite">
      {items.map((t) => {
        const a = t.activity;
        const waiting = a?.mode === "ai_wait";
        const pct = t.total ? Math.round((Math.min(t.done, t.total) / t.total) * 100) : 0;
        const line =
          t.state === "running"
            ? a
              ? waiting
                ? a.step.replace("The AI provider is rate-limiting us", "The AI asked us to slow down")
                : a.step
              : "Starting…"
            : t.state === "done"
              ? t.summary ?? "Done"
              : t.state === "failed"
                ? `Didn't finish: ${t.error ?? "unknown error"}`
                : "Lost track of this one - the server restarted. Click to see what's in your uploads.";
        return (
          <div key={t.key} className={`task-card task-${t.state} ${waiting ? "task-waiting" : ""}`}>
            <button className="task-main" onClick={() => onOpen(t)} title="Open">
              <span className="task-icon">
                {t.state === "running" ? <span className="live-spinner" /> : t.state === "done" ? "✓" : t.state === "failed" ? "✕" : "!"}
              </span>
              <span className="task-text">
                <strong>{t.label}</strong>
                <span>{line}</span>
                {t.state === "running" && t.total > 1 && (
                  <span className="task-progress">
                    <span className="progress"><span className="progress-bar" style={{ width: `${pct}%` }} /></span>
                    <small>{Math.min(t.done, t.total)} / {t.total}</small>
                  </span>
                )}
              </span>
              <span className="task-go">{t.state === "running" ? "View" : "Open"} →</span>
            </button>
            {t.state !== "running" && (
              <button className="task-close" title="Dismiss" onClick={() => onDismiss(t.key)}>✕</button>
            )}
          </div>
        );
      })}
    </div>
  );
}

function Sidebar({
  page,
  setPage,
  busyLabel,
}: {
  page: Page;
  setPage: (page: Page) => void;
  busyLabel: string | null;
}) {
  return (
    <aside className="sidebar">
    <div className="brand">
      <img
        src={cargoSenseLogo}
        alt="CargoSense"
        className="brand-logo"
      />
    </div>

      <nav>
        <button
          className={`nav-item ${
            page === "dashboard" ? "active" : ""
          }`}
          onClick={() => setPage("dashboard")}
        >
          Dashboard
        </button>

        <button
          className={`nav-item ${
            page === "inbox" ||
            page === "comparison"
              ? "active"
              : ""
          }`}
          onClick={() => setPage("inbox")}
        >
          Inbox
        </button>

        <button
          className={`nav-item ${
            page === "review" ? "active" : ""
          }`}
          onClick={() => setPage("review")}
        >
          Human Review
        </button>

        <button
          className={`nav-item ${
            page === "check" ? "active" : ""
          }`}
          onClick={() => setPage("check")}
        >
          Upload & Check
        </button>
      </nav>

      <div className="sidebar-footer">
        <SystemStatus busyLabel={busyLabel} />
      </div>
    </aside>
  );
}

function RunControls({
  run,
  runTask,
  onRun,
}: {
  run: RunStatus | null;
  runTask: Task | undefined;
  onRun: (opts: { retry?: boolean; no_ai?: boolean }) => void;
}) {
  if (runTask) {
    const pct = runTask.total ? Math.round((runTask.done / runTask.total) * 100) : 0;
    return (
      <div className="run-controls">
        <div className="ai-badge">
          <span className="status-dot"></span>
          Processing {runTask.done}/{runTask.total || "…"}
        </div>
        <div className="progress run-progress">
          <div className="progress-bar" style={{ width: `${pct}%` }}></div>
        </div>
        {runTask.activity && (
          <span className="run-step">
            {MODE_LABEL[runTask.activity.mode]} · {runTask.activity.step}
          </span>
        )}
      </div>
    );
  }
  return (
    <div className="run-controls">
      <button className="primary-button" onClick={() => onRun({})}>
        Run pipeline
      </button>
      <button className="secondary-button" onClick={() => onRun({ retry: true })}>
        Retry failures
      </button>
      {run?.error && <span className="run-error">Last run failed: {run.error}</span>}
    </div>
  );
}

function BasisTag({ row }: { row: EmailRow }) {
  return (
    <span className={`basis-tag basis-${row.basis.kind}`}>
      {row.basis.label}
    </span>
  );
}

function Dashboard({
  summary,
  emails,
  run,
  runTask,
  onRun,
  onViewInbox,
  onOpenEmail,
  onOpenReview,
}: {
  summary: Summary | null;
  emails: EmailRow[];
  run: RunStatus | null;
  runTask: Task | undefined;
  onRun: (opts: { retry?: boolean; no_ai?: boolean }) => void;
  onViewInbox: (filter?: InboxFilter) => void;
  onOpenEmail: (id: string) => void;
  onOpenReview: () => void;
}) {
  // What needs attention first: open reviews, then mismatches
  const recentActivity = useMemo(
    () =>
      [...emails]
        .filter((e) => e.category === "BL_COMPARISON" && e.source !== "upload")
        .sort(
          (a, b) =>
            Number(b.needs_review) - Number(a.needs_review) ||
            Number(b.status === "MISMATCH") - Number(a.status === "MISMATCH"),
        )
        .slice(0, 6),
    [emails],
  );

  const s = summary && summary.has_results ? summary : null;
  const pct = (x: number) => `${(x * 100).toFixed(1)}%`;

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">Shipping Operations</p>
          <h2>Document Verification Dashboard</h2>
          <p className="page-subtitle">The provided inbox, processed: every email classified, every SI checked against its draft BL.</p>
        </div>
        <RunControls run={run} runTask={runTask} onRun={onRun} />
      </header>

      <WelcomeBanner onViewInbox={() => onViewInbox()} onOpenReview={onOpenReview} />

      {!s ? (
        <section className="panel empty-state">
          <strong>No results yet</strong>
          <span>
            Run the pipeline to classify the inbox and check every SI / BL pair.
          </span>
        </section>
      ) : (
        <>
          <section className="stats-grid">
            <StatCard
              title="Emails Processed"
              value={String(s.emails_processed)}
              description={`${s.comparisons} document-check requests`}
            />
            <StatCard
              title="Discrepancies"
              value={String(s.mismatches)}
              description={
                s.comparisons
                  ? `${pct(s.mismatches / s.comparisons)} of checks`
                  : "no checks yet"
              }
            />
            <StatCard
              title="Human Review"
              value={String(s.needs_review)}
              description={
                s.resolved_by_reviewer
                  ? `${s.resolved_by_reviewer} resolved by reviewers`
                  : "Requires attention"
              }
            />
            <StatCard
              title="Automation Rate"
              value={pct(s.automation_rate)}
              description="Decided without a human"
            />
          </section>

          <section className="content-grid">
            <div className="panel activity-panel">
              <div className="panel-header">
                <div>
                  <p className="eyebrow">Needs Attention First</p>
                  <h3>Document Checks</h3>
                </div>
                <button className="secondary-button" onClick={() => onViewInbox()}>
                  View Inbox
                </button>
              </div>

              <div className="activity-list">
                {recentActivity.map((item) => (
                  <button
                    className="activity-row"
                    key={item.id}
                    onClick={() => onOpenEmail(item.id)}
                  >
                    <div className="email-info">
                      <div className="email-icon">✉</div>
                      <div>
                        <strong>{item.subject}</strong>
                        <span>{item.id}</span>
                      </div>
                    </div>
                    <div className="activity-result">
                      <StatusBadge status={statusLabel(item.category, item.status)} />
                      <BasisTag row={item} />
                    </div>
                  </button>
                ))}
              </div>
            </div>

            <div className="panel performance-panel">
              <div>
                <p className="eyebrow">Pipeline</p>
                <h3>Processing Overview</h3>
              </div>

              <div className="performance-stat">
                <div>
                  <span>Automatic Processing</span>
                  <strong>{pct(s.automation_rate)}</strong>
                </div>
                <div className="progress">
                  <div
                    className="progress-bar automation-bar"
                    style={{ width: pct(s.automation_rate) }}
                  ></div>
                </div>
              </div>

              <div className="performance-stat">
                <div>
                  <span>Fields cross-checked (rules + AI agree)</span>
                  <strong>
                    {s.cross_checked_rate === null ? "Rules only" : pct(s.cross_checked_rate)}
                  </strong>
                </div>
                {s.cross_checked_rate === null && (
                  <p className="review-note cross-note">
                    This inbox was read by the rule-based parser alone (no AI needed). The AI
                    cross-check is used for what you upload.
                  </p>
                )}
                <div className="progress">
                  <div
                    className="progress-bar confidence-bar"
                    style={{ width: pct(s.cross_checked_rate ?? 0) }}
                  ></div>
                </div>
              </div>

              <div className="mini-stats">
                <div>
                  <strong>{s.no_mismatch}</strong>
                  <span>No Mismatch</span>
                </div>
                <div>
                  <strong>{s.retryable_failures}</strong>
                  <span>Failed (retryable)</span>
                </div>
              </div>
            </div>
          </section>

          {s.uploads.emails > 0 && (
            <UploadsPanel
              uploads={s.uploads}
              recent={emails.filter((e) => e.source === "upload").slice(0, 4)}
              onViewInbox={onViewInbox}
              onOpenEmail={onOpenEmail}
              onOpenReview={onOpenReview}
            />
          )}
        </>
      )}
    </>
  );
}

type InboxFilter = "All" | Category | "Uploaded";

const WELCOME_KEY = "sdoc_welcome_dismissed";
const REVIEWER_KEY = "sdoc_reviewer_name";   // remembered so reviewers don't retype it

// First-visit guide. Dismissal is remembered in this browser.
function WelcomeBanner({ onViewInbox, onOpenReview }: { onViewInbox: () => void; onOpenReview: () => void }) {
  const [hidden, setHidden] = useState(() => {
    try {
      return localStorage.getItem(WELCOME_KEY) === "1";
    } catch {
      return false;
    }
  });
  if (hidden) return null;
  const dismiss = () => {
    try {
      localStorage.setItem(WELCOME_KEY, "1");
    } catch {
      /* private mode: just hide for now */
    }
    setHidden(true);
  };
  return (
    <section className="panel welcome-banner">
      <div className="welcome-text">
        <p className="eyebrow">New here? A 1-minute tour</p>
        <ol>
          <li>
            <button className="link-button" onClick={onViewInbox}>Inbox</button>: 520 emails, sorted
            into 5 categories. Click any email to see why it was classified that way.
          </li>
          <li>
            Open a <b>BL Comparison</b> email: the 7 fields of the SI and the draft BL side by side,
            with any mismatch highlighted.
          </li>
          <li>
            <button className="link-button" onClick={onOpenReview}>Human Review</button>: what the
            system wouldn't decide alone (scans, blanks, wrong documents), with the evidence.
          </li>
          <li>
            <b>Upload & Check</b>: try your own emails and documents, or download our sample inbox
            and upload it back.
          </li>
        </ol>
      </div>
      <button className="secondary-button" onClick={dismiss}>Got it</button>
    </section>
  );
}

function UploadsPanel({
  uploads,
  recent,
  onViewInbox,
  onOpenEmail,
  onOpenReview,
}: {
  uploads: UploadSummary;
  recent: EmailRow[];
  onViewInbox: (filter?: InboxFilter) => void;
  onOpenEmail: (id: string) => void;
  onOpenReview: () => void;
}) {
  return (
    <section className="panel uploads-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Upload & Check</p>
          <h3>Your uploads</h3>
          <p className="review-note uploads-note">
            Kept separate from the numbers above, which describe the provided inbox.
          </p>
        </div>
        <div className="uploads-actions">
          {uploads.needs_review > 0 && (
            <button className="secondary-button" onClick={onOpenReview}>
              Review ({uploads.needs_review})
            </button>
          )}
          <button className="secondary-button" onClick={() => onViewInbox("Uploaded")}>
            View uploads
          </button>
        </div>
      </div>
      <div className="uploads-stats">
        <div><strong>{uploads.emails}</strong><span>Emails uploaded{uploads.batches ? ` (${uploads.batches} batch${uploads.batches === 1 ? "" : "es"})` : ""}</span></div>
        <div><strong>{uploads.comparisons}</strong><span>Document checks</span></div>
        <div><strong>{uploads.no_mismatch}</strong><span>No mismatch</span></div>
        <div><strong>{uploads.mismatches}</strong><span>Discrepancies</span></div>
        <div><strong>{uploads.needs_review}</strong><span>Need review</span></div>
      </div>
      {recent.length > 0 && (
        <div className="activity-list">
          {recent.map((item) => (
            <button
              className="activity-row"
              key={item.id}
              onClick={() => (item.category === "BL_COMPARISON" ? onOpenEmail(item.id) : onViewInbox("Uploaded"))}
            >
              <div className="email-info">
                <div className="email-icon">✉</div>
                <div>
                  <strong>{item.subject || "(no subject)"}</strong>
                  <span>{item.original_id ?? item.id}</span>
                </div>
              </div>
              <div className="activity-result">
                <CategoryBadge category={CATEGORY_LABEL[item.category]} />
                <StatusBadge status={statusLabel(item.category, item.status)} />
              </div>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

function Inbox({
  emails,
  initialFilter = "All",
  onOpenEmail,
  onChanged,
}: {
  emails: EmailRow[];
  initialFilter?: InboxFilter;
  onOpenEmail: (id: string) => void;
  onChanged: () => void;
}) {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<InboxFilter>(initialFilter);
  const [notice, setNotice] = useState<string | null>(null);
  const uploadedCount = emails.filter((e) => e.source === "upload").length;

  const removeOne = async (email: EmailRow) => {
    const r = await confirmDelete(
      `Delete "${email.subject || email.id}" and its attachments?`,
      () => api.deleteEmail(email.id),
    );
    if (r) {
      setNotice(deletedMessage(r));
      onChanged();
    }
  };

  const removeAll = async () => {
    const r = await confirmDelete(
      `Delete all ${uploadedCount} uploaded emails and their attachments? The provided dataset is not affected.`,
      () => api.deleteAllUploads(),
    );
    if (r) {
      setNotice(deletedMessage(r));
      setFilter("All");
      onChanged();
    }
  };

  const filteredEmails = useMemo(() => {
    const q = search.toLowerCase();
    return emails.filter((email) => {
      const matchesSearch =
        email.subject.toLowerCase().includes(q) ||
        email.sender.toLowerCase().includes(q) ||
        email.id.toLowerCase().includes(q) ||
        (email.original_id ?? "").toLowerCase().includes(q);
      const matchesFilter =
        filter === "All" ||
        (filter === "Uploaded" ? email.source === "upload" : CATEGORY_LABEL[email.category] === filter);
      return matchesSearch && matchesFilter;
    });
  }, [emails, search, filter]);

  const filters: InboxFilter[] = [
    "All",
    "BL Comparison",
    "SI Request",
    "Invoice Query",
    "General",
    "Spam",
    ...(uploadedCount ? (["Uploaded"] as InboxFilter[]) : []),
  ];
  const filterCount = (f: InboxFilter) =>
    f === "All"
      ? emails.length
      : f === "Uploaded"
        ? uploadedCount
        : emails.filter((e) => CATEGORY_LABEL[e.category] === f).length;

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">AI Mail Processing</p>
          <h2>Inbox</h2>
          <p className="page-subtitle">Every email, sorted into 5 categories. Click one to see why, and for document checks the SI ↔ BL comparison.</p>
        </div>
        <div className="ai-badge">
          <span className="status-dot"></span>
          {emails.length} emails loaded
        </div>
      </header>

      <section className="panel inbox-panel">
        <div className="inbox-toolbar">
          <div className="search-wrapper">
            <span>⌕</span>
            <input
              type="text"
              placeholder="Search emails, sender or ID..."
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
          <span className="result-count">{filteredEmails.length} results</span>
          {uploadedCount > 0 && (
            <button className="secondary-button danger-button" onClick={removeAll}>
              Delete all uploads ({uploadedCount})
            </button>
          )}
        </div>
        {notice && <p className="review-note notice">{notice}</p>}

        <div className="filter-row">
          {filters.map((item) => (
            <button
              key={item}
              className={`filter-button ${filter === item ? "active-filter" : ""}`}
              onClick={() => setFilter(item)}
            >
              {item} <span className="filter-count">{filterCount(item)}</span>
            </button>
          ))}
        </div>

        <div className="inbox-table">
          <div className="inbox-table-header">
            <span>Email</span>
            <span>Category</span>
            <span>Status</span>
            <span>Verified by</span>
            <span>Documents</span>
          </div>

          {filteredEmails.map((email) => {
            return (
              <div
                role="button"
                tabIndex={0}
                className="inbox-row"
                key={email.id}
                onClick={() => onOpenEmail(email.id)}
                onKeyDown={(e) => e.key === "Enter" && onOpenEmail(email.id)}
              >
                <div className="inbox-email">
                  <div className="email-icon">✉</div>
                  <div>
                    <strong>{email.subject || "(no subject)"}</strong>
                    <span>
                      {email.sender || "unknown sender"} · {email.original_id ?? email.id}
                    </span>
                    {email.source === "upload" && (
                      <small>{email.batch_id ? "Uploaded in a batch" : "Uploaded"}</small>
                    )}
                    {email.reviewed && <small>Reviewed by a person</small>}
                  </div>
                </div>
                <CategoryBadge category={CATEGORY_LABEL[email.category]} />
                <StatusBadge status={statusLabel(email.category, email.status)} />
                <div className="confidence-cell">
                  <BasisTag row={email} />
                </div>
                <span className="received-time">
                  {email.attachments
                    ? email.attachment_types.join(" + ").toUpperCase()
                    : "—"}
                  {email.source === "upload" && (
                    <button
                      className="row-delete"
                      title="Delete this uploaded email and its attachments"
                      onClick={(e) => {
                        e.stopPropagation();
                        removeOne(email);
                      }}
                    >
                      ✕
                    </button>
                  )}
                </span>
              </div>
            );
          })}

          {filteredEmails.length === 0 && (
            <div className="empty-state">
              <strong>No emails found</strong>
              <span>
                {emails.length
                  ? "Try a different search or filter."
                  : "Run the pipeline from the dashboard first."}
              </span>
            </div>
          )}
        </div>
      </section>
    </>
  );
}

function useEmail(emailId: string | null) {
  const [detail, setDetail] = useState<EmailDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!emailId) return;
    let cancelled = false;
    api
      .email(emailId)
      .then((d) => !cancelled && (setDetail(d), setError(null)))
      .catch((err) => !cancelled && setError(errorText(err)));
    return () => {
      cancelled = true;
    };
  }, [emailId]);
  return { detail, setDetail, error };
}

function ComparisonPage({
  emailId,
  backLabel,
  onBack,
  onReview,
  onDeleted,
}: {
  emailId: string;
  backLabel: string;
  onBack: () => void;
  onReview: () => void;
  onDeleted: () => void;
}) {
  const { detail, error } = useEmail(emailId);

  const removeEmail = async () => {
    if (await confirmDelete("Delete this uploaded email and its attachments?", () => api.deleteEmail(emailId)))
      onDeleted();
  };
  const removeBatch = async (batchId: string) => {
    if (await confirmDelete("Delete this whole uploaded batch (every email and file in it)?",
                            () => api.deleteBatch(batchId)))
      onDeleted();
  };

  if (error)
    return (
      <>
        <button className="back-button" onClick={onBack}>← Back to {backLabel}</button>
        <section className="panel empty-state"><strong>Couldn't load this email</strong><span>{error}</span></section>
      </>
    );
  if (!detail || detail.id !== emailId) return <Loading text="Loading the email…" />;

  const fields = detail.view.fields;
  const mismatched = fields.filter((f) => f.match === false);
  const status = statusLabel(detail.category, detail.status);
  const docs = detail.documents;
  const isComparison = detail.category === "BL_COMPARISON";

  return (
    <>
      <button className="back-button" onClick={onBack}>
        ← Back to {backLabel}
      </button>

      <header className="comparison-header">
        <div>
          <p className="eyebrow">{isComparison ? "Document Verification" : "Email"}</p>
          <h2>{detail.subject || "(no subject)"}</h2>
          <p className="comparison-subtitle">
            {detail.original_id ?? detail.id} · {detail.sender || "unknown sender"}
            {detail.source === "upload" ? " · uploaded" : ""}
          </p>
        </div>
        <div className="comparison-header-right">
          <StatusBadge status={status} />
          <span className="comparison-confidence">{detail.basis.label}</span>
          {detail.source === "upload" && (
            <div className="delete-actions">
              <button className="secondary-button danger-button" onClick={removeEmail}>
                Delete email
              </button>
              {detail.batch_id && (
                <button className="secondary-button danger-button" onClick={() => removeBatch(detail.batch_id!)}>
                  Delete batch
                </button>
              )}
            </div>
          )}
        </div>
      </header>

      {!isComparison && (
        <section className="comparison-summary summary-info">
          <div className="summary-icon">i</div>
          <div>
            <strong>Classified as {CATEGORY_LABEL[detail.category]}</strong>
            <p>
              {explainClassification(detail.classification?.method)} Only document-comparison
              requests go on to the SI ↔ BL check.
            </p>
          </div>
        </section>
      )}
      {isComparison && detail.status === "OK" && (
        <section className="comparison-summary summary-ok">
          <div className="summary-icon">✓</div>
          <div>
            <strong>{fields.length ? "No mismatch detected." : "Nothing to compare yet"}</strong>
            <p>
              {fields.length
                ? "All seven fields agree between the Shipping Instruction and the draft Bill of Lading."
                : detail.note ?? "No documents to compare."}
            </p>
          </div>
        </section>
      )}
      {detail.status === "MISMATCH" && (
        <section className="comparison-summary summary-mismatch">
          <div className="summary-icon">!</div>
          <div>
            <strong>
              {detail.defect_fields.length} discrepanc
              {detail.defect_fields.length === 1 ? "y" : "ies"} detected
            </strong>
            <p>The draft Bill of Lading does not fully match the Shipping Instruction.</p>
          </div>
        </section>
      )}
      {detail.status === "NEEDS_REVIEW" && (
        <section className="comparison-summary summary-review">
          <div className="summary-icon">?</div>
          <div>
            <strong>
              Needs human review —{" "}
              {REVIEW_REASON_LABEL[detail.review_reason ?? ""] ?? detail.review_reason}
            </strong>
            <p>{detail.note}</p>
          </div>
          {detail.needs_review && (
            <button className="primary-button" onClick={onReview}>
              Open in review
            </button>
          )}
        </section>
      )}

      {fields.length > 0 && (
        <section className="panel comparison-panel">
          <div className="comparison-title-row">
            <div>
              <p className="eyebrow">Field Verification</p>
              <h3>SI ↔ BL Comparison</h3>
            </div>
            <span className="field-count">
              {detail.view.source === "proposed"
                ? "Proposed — not yet confirmed"
                : detail.view.source === "reviewer"
                  ? "Corrected by a reviewer"
                  : `${fields.length} fields checked`}
            </span>
          </div>
          <FieldTable fields={fields} />
        </section>
      )}

      {isComparison && (
      <section className="comparison-bottom-grid">
        <div className="panel discrepancy-panel">
          <p className="eyebrow">Attention Required</p>
          <h3>Detected Discrepancies</h3>
          {mismatched.length === 0 && (
            <p className="review-note">No field-level discrepancies.</p>
          )}
          {mismatched.map((f) => (
            <div className="discrepancy-item" key={f.field}>
              <strong>{f.label}</strong>
              <div className="difference-values">
                <div>
                  <span>Shipping Instruction</span>
                  <p>{formatValue(f.si)}</p>
                </div>
                <div>
                  <span>Bill of Lading</span>
                  <p>{formatValue(f.bl)}</p>
                </div>
              </div>
            </div>
          ))}
        </div>

        <div className="panel processing-panel">
          <p className="eyebrow">Processing</p>
          <h3>Verification Details</h3>
          <div className="processing-line">
            <span>Email classification</span>
            <strong>{CATEGORY_LABEL[detail.category]}</strong>
          </div>
          {detail.classification?.method && (
            <p className="review-note why-note">{explainClassification(detail.classification.method)}</p>
          )}
          <div className="processing-line">
            <span>Documents detected</span>
            <strong>{detail.attachments} attached</strong>
          </div>
          {docs.map((d) => (
            <div className="processing-line" key={d.side}>
              <span>{d.side} read as</span>
              <strong>
                {d.file_type?.toUpperCase()} · {d.read_method ?? "not read"}
                {d.ocr ? ` (${Math.round(d.ocr.confidence * 100)}%)` : ""}
              </strong>
            </div>
          ))}
          <div className="processing-line">
            <span>Fields compared</span>
            <strong>
              {fields.filter((f) => f.match !== null).length} / 7
            </strong>
          </div>
          <div className="processing-line">
            <span>Human review</span>
            <strong>
              {detail.decision
                ? `Resolved by ${detail.decision.reviewer ?? "reviewer"}`
                : detail.needs_review
                  ? "Required"
                  : "Not required"}
            </strong>
          </div>
          <AttachmentLinks detail={detail} />
        </div>
      </section>
      )}

      <EmailBody detail={detail} open={!isComparison} onReview={onReview} />
    </>
  );
}

// What the sender wrote (external-sender banners and the forwarded thread are left out).
function EmailBody({
  detail,
  open,
  onReview,
}: {
  detail: EmailDetail;
  open: boolean;
  onReview: () => void;
}) {
  const body = (detail.body_preview ?? "").trim();
  return (
    <section className="panel email-body-panel">
      <details open={open}>
        <summary>
          <span className="eyebrow">The email</span>
          <span className="email-meta">
            From {detail.sender || "unknown sender"}
            {detail.attachments ? ` · ${detail.attachments} attachment${detail.attachments === 1 ? "" : "s"}` : " · no attachments"}
          </span>
        </summary>
        {body ? <pre className="email-body">{body}</pre> : <p className="review-note">(no text)</p>}
        {!(detail.category === "BL_COMPARISON") && <AttachmentLinks detail={detail} />}
        {detail.needs_review && detail.category !== "BL_COMPARISON" && (
          <button className="primary-button" onClick={onReview}>Open in review</button>
        )}
      </details>
    </section>
  );
}

function Loading({ text }: { text: string }) {
  return (
    <section className="panel loading-state">
      <span className="live-spinner" />
      <span>{text}</span>
    </section>
  );
}

function AttachmentLinks({ detail }: { detail: EmailDetail }) {
  if (!detail.attachments) return null;
  const paths = detail.documents.map((d) => d.path);
  return (
    <div className="attachment-links">
      {Array.from({ length: detail.attachments }, (_, i) => (
        <a
          key={i}
          href={api.attachmentUrl(detail.id, i)}
          target="_blank"
          rel="noreferrer"
          className="secondary-button"
        >
          Open {paths[i]?.split("/").pop() ?? `attachment ${i + 1}`}
        </a>
      ))}
    </div>
  );
}

function FieldTable({ fields }: { fields: FieldRow[] }) {
  return (
    <div className="comparison-table">
      <div className="comparison-table-header">
        <span>Field</span>
        <span>Shipping Instruction</span>
        <span>Bill of Lading</span>
        <span>Result</span>
      </div>
      {fields.map((f) => (
        <div
          className={`comparison-row ${f.match === false ? "comparison-mismatch" : ""}`}
          key={f.field}
        >
          <strong>{f.label}</strong>
          <span title={f.si_evidence ?? undefined}>
            {formatValue(f.si)}
            {f.si_source && <small className="source-note">{SOURCE_LABEL[f.si_source] ?? f.si_source}</small>}
          </span>
          <span title={f.bl_evidence ?? undefined}>
            {formatValue(f.bl)}
            {f.bl_source && <small className="source-note">{SOURCE_LABEL[f.bl_source] ?? f.bl_source}</small>}
          </span>
          <span
            className={
              f.match === null
                ? "field-result"
                : f.match
                  ? "field-result match"
                  : "field-result mismatch"
            }
          >
            {f.match === null
              ? "— Not compared"
              : f.match
                ? "✓ Match"
                : f.method === "missing_value"
                  ? "? Missing"
                  : "⚠ Mismatch"}
          </span>
        </div>
      ))}
    </div>
  );
}

const ACCEPT = ".txt,.pdf,.docx,.xlsx,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp";
const BATCH_ACCEPT = `.zip,.json,.eml,${ACCEPT}`;

type SingleDraft = {
  subject: string;
  sender: string;
  body: string;
  si: File | null;
  bl: File | null;
};

const EMPTY_DRAFT: SingleDraft = { subject: "", sender: "", body: "", si: null, bl: null };

function CheckPage({
  mode,
  setMode,
  store,
  batchFiles,
  setBatchFiles,
  draft,
  setDraft,
  onOpenEmail,
  onReview,
  onChanged,
}: {
  mode: "batch" | "single";
  setMode: (m: "batch" | "single") => void;
  store: TaskStore;
  batchFiles: File[];
  setBatchFiles: (f: File[]) => void;
  draft: SingleDraft;
  setDraft: (d: SingleDraft) => void;
  onOpenEmail: (id: string) => void;
  onReview: (id: string) => void;
  onChanged: () => void;
}) {
  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">Try It Yourself</p>
          <h2>Upload & Check</h2>
          <p className="page-subtitle">Run the system on your own emails and SI / BL documents. Your uploads stay separate from the provided inbox, and keep running if you visit other pages.</p>
        </div>
        <div className="filter-row check-tabs">
          <button className={`filter-button ${mode === "batch" ? "active-filter" : ""}`} onClick={() => setMode("batch")}>
            Whole inbox
          </button>
          <button className={`filter-button ${mode === "single" ? "active-filter" : ""}`} onClick={() => setMode("single")}>
            Single email
          </button>
        </div>
      </header>
      {mode === "batch" ? (
        <BatchCheck store={store} files={batchFiles} setFiles={setBatchFiles} onOpenEmail={onOpenEmail} onChanged={onChanged} />
      ) : (
        <SingleCheck store={store} draft={draft} setDraft={setDraft} onOpenEmail={onOpenEmail} onReview={onReview} onChanged={onChanged} />
      )}
    </>
  );
}

function when(iso: string | null) {
  if (!iso) return "";
  const d = new Date(iso.length === 15 ? `${iso.slice(0, 4)}-${iso.slice(4, 6)}-${iso.slice(6, 8)}T${iso.slice(9, 11)}:${iso.slice(11, 13)}:${iso.slice(13, 15)}` : iso);
  return isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function BatchCheck({
  store,
  files,
  setFiles,
  onOpenEmail,
  onChanged,
}: {
  store: TaskStore;
  files: File[];
  setFiles: (f: File[]) => void;
  onOpenEmail: (id: string) => void;
  onChanged: () => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [starting, setStarting] = useState(false);
  const [recent, setRecent] = useState<BatchSummary[] | null>(null);

  const current = store.batchId;
  const task = store.tasks.find((t) => t.kind === "batch" && t.id === current);
  const running = task?.state === "running";
  const view = store.batchView && store.batchView.id === current ? store.batchView : null;
  const anyBatchRunning = store.tasks.some((t) => t.kind === "batch" && t.state === "running");

  // recent batches from the server: works after a reload or in a new tab too
  const doneCount = store.tasks.filter((t) => t.kind === "batch" && t.state !== "running").length;
  useEffect(() => {
    let alive = true;
    api.batches().then((b) => alive && setRecent(b)).catch(() => alive && setRecent([]));
    return () => {
      alive = false;
    };
  }, [doneCount, current]);

  const addFiles = (list: FileList | null) => {
    if (list) setFiles([...files, ...Array.from(list)]);
  };

  const start = async () => {
    setError(null);
    setStarting(true);
    try {
      await store.startBatch(files);
      setFiles([]);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setStarting(false);
    }
  };

  const rows = view?.rows ?? [];
  const others = (recent ?? []).filter((b) => b.id !== current);

  return (
    <section className="check-grid">
      <div className="panel">
        <p className="eyebrow">Your inbox</p>
        <h3>Upload many emails at once</h3>
        <div
          className={`dropzone ${dragging ? "dropzone-active" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            addFiles(e.dataTransfer.files);
          }}
        >
          <strong>Drop files here</strong>
          <span>or</span>
          <label className="secondary-button">
            Choose files
            <input type="file" multiple accept={BATCH_ACCEPT} hidden onChange={(e) => { addFiles(e.target.files); e.target.value = ""; }} />
          </label>
        </div>
        <p className="review-note">
          A <b>.zip</b> of an inbox (inbox/*.json + attachments/, same layout as the dataset),
          email <b>.json</b> files, or saved <b>.eml</b> emails, plus their attachment files
          (TXT, PDF, Word, Excel, scans). Attachments are matched to emails by file name, and SI / BL
          files need <b>SI</b> / <b>BL</b> in their names.
        </p>

        {files.length > 0 && (
          <div className="file-list">
            {files.map((f, i) => (
              <div key={`${f.name}-${i}`} className="file-chip">
                <span>{f.name}</span>
                <small>{(f.size / 1024).toFixed(0)} KB</small>
                <button className="chip-remove" title="Remove" onClick={() => setFiles(files.filter((_, j) => j !== i))}>✕</button>
              </div>
            ))}
            <button className="secondary-button" onClick={() => setFiles([])}>Clear</button>
          </div>
        )}

        <button
          className="primary-button resolve-button"
          disabled={starting || anyBatchRunning || files.length === 0}
          onClick={start}
        >
          {starting ? "Uploading…" : anyBatchRunning ? "A batch is running - see the progress →" : "Check inbox"}
        </button>
        {error && <p className="run-error">{error}</p>}

        <p className="review-note sample-note">
          No test files?{" "}
          <a href={api.sampleBundleUrl()} download>Download a sample inbox (20 emails)</a>{" "}
          and upload it here.
        </p>

        {others.length > 0 && (
          <div className="recent-batches">
            <p className="eyebrow">Your earlier batches</p>
            {others.slice(0, 6).map((b) => (
              <div key={b.id} className="recent-batch">
                <div>
                  <strong>{plural(b.emails || b.total, "email")}</strong>
                  <span>
                    {b.running
                      ? `running… ${b.done}/${b.total}`
                      : b.error
                        ? `interrupted after ${b.emails} of ${b.total}`
                        : `${when(b.finished ?? b.started)} · ${b.mismatches} mismatch${b.mismatches === 1 ? "" : "es"}, ${b.needs_review} for review`}
                  </span>
                </div>
                <button className="secondary-button" onClick={() => store.openBatch(b.id)}>Open</button>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="panel">
        <div className="results-head">
          <p className="eyebrow">Results</p>
          {current && !running && (
            <button className="link-button" onClick={store.closeBatch}>Close</button>
          )}
        </div>
        {!current ? (
          <div className="empty-state">
            <strong>No batch open</strong>
            <span>Upload an inbox, then press Check inbox{others.length ? ", or open one of your earlier batches" : ""}.</span>
          </div>
        ) : task?.state === "lost" ? (
          <div className="empty-state">
            <strong>We lost track of this batch</strong>
            <span>The server was restarted, so its results are gone. Please upload it again.</span>
          </div>
        ) : running || !view ? (
          <LiveStatus
            activity={task?.activity}
            fallback={running ? `Processing ${task?.done ?? 0} of ${task?.total ?? "…"} emails…` : "Loading the results…"}
          />
        ) : (
          <>
            <p className="done-line">
              {batchSummary(view)}
              {view.activity ? ` in ${Math.round(view.activity.elapsed)}s` : ""}.
            </p>
            {view.error && <p className="run-error">{view.error}</p>}
            {view.warnings.length > 0 && (
              <details className="batch-warnings">
                <summary>{view.warnings.length} note(s) about the upload</summary>
                {view.warnings.map((w) => <p key={w} className="review-note">⚠ {w}</p>)}
              </details>
            )}
            {view.counts && (
              <div className="batch-counts">
                {Object.entries(view.counts).map(([k, v]) => (
                  <span key={k} className="basis-tag">{k.replace("BL_COMPARISON/", "BL · ").replace(/_/g, " ")}: {v}</span>
                ))}
              </div>
            )}
            {rows.length > 0 && (
              <>
                <div className="batch-actions">
                  <a className="primary-button download-button" href={api.batchSubmissionUrl(view.id)} download>
                    Download submission.json
                  </a>
                  <button
                    className="secondary-button danger-button"
                    onClick={async () => {
                      const r = await confirmDelete(`Delete this batch (${rows.length} emails and their files)?`, () => api.deleteBatch(view.id));
                      if (r) {
                        store.closeBatch();
                        onChanged();
                      }
                    }}
                  >
                    Delete this batch
                  </button>
                </div>
                <div className="batch-table">
                  {rows.map((r) => (
                    <button key={r.id} className="batch-row" onClick={() => onOpenEmail(r.id)}>
                      <span className="batch-id">{r.original_id ?? r.id}</span>
                      <span className="batch-subject">{r.subject || "(no subject)"}</span>
                      <CategoryBadge category={CATEGORY_LABEL[r.category]} />
                      <StatusBadge status={statusLabel(r.category, r.status)} />
                      <span className="batch-detail">
                        {r.status === "MISMATCH"
                          ? r.defect_fields.map(fieldLabel).join(", ")
                          : r.status === "NEEDS_REVIEW"
                            ? (REVIEW_REASON_LABEL[r.review_reason ?? ""] ?? r.review_reason)
                            : ""}
                      </span>
                    </button>
                  ))}
                </div>
              </>
            )}
          </>
        )}
      </div>
    </section>
  );
}

function plural(n: number, word: string, many = `${word}s`) {
  return `${n} ${n === 1 ? word : many}`;
}

function SingleCheck({
  store,
  draft,
  setDraft,
  onOpenEmail,
  onReview,
  onChanged,
}: {
  store: TaskStore;
  draft: SingleDraft;
  setDraft: (d: SingleDraft) => void;
  onOpenEmail: (id: string) => void;
  onReview: (id: string) => void;
  onChanged: () => void;
}) {
  const task = [...store.tasks].reverse().find((t) => t.kind === "check");
  const busy = task?.state === "running";
  const result = store.checkResult;
  const { subject, sender, body, si, bl } = draft;
  const set = (patch: Partial<SingleDraft>) => setDraft({ ...draft, ...patch });
  const canSubmit = !busy && (si || bl || body.trim() || subject.trim());

  const submit = async () => {
    const id = newJobId();
    const form = new FormData();
    form.append("job_id", id);
    form.append("subject", subject);
    form.append("sender", sender);
    form.append("body", body);
    if (si) form.append("si", si);
    if (bl) form.append("bl", bl);
    await store.startCheck(form, id, subject.trim() || (si || bl ? "Your SI / BL check" : "Your email"));
  };

  const fileField = (label: string, file: File | null, key: "si" | "bl") => (
    <>
      <label className="review-label">{label}</label>
      {file ? (
        <div className="file-chip picked">
          <span>{file.name}</span>
          <small>{(file.size / 1024).toFixed(0)} KB</small>
          <button className="chip-remove" title="Remove" onClick={() => set({ [key]: null } as Partial<SingleDraft>)}>✕</button>
        </div>
      ) : (
        <input className="review-input" type="file" accept={ACCEPT}
               onChange={(e) => set({ [key]: e.target.files?.[0] ?? null } as Partial<SingleDraft>)} />
      )}
    </>
  );

  return (
    <section className="check-grid">
      <div className="panel">
        <p className="eyebrow">Email</p>
        <h3>What arrived in the inbox</h3>
        <label className="review-label">Subject</label>
        <input className="review-input" value={subject} onChange={(e) => set({ subject: e.target.value })}
               placeholder="e.g. TO CONFIRM DOCS - 5RSG-51584" />
        <label className="review-label">From</label>
        <input className="review-input" value={sender} onChange={(e) => set({ sender: e.target.value })}
               placeholder="sender@company.com" />
        <label className="review-label">Body</label>
        <textarea className="review-input check-body" value={body} onChange={(e) => set({ body: e.target.value })}
                  placeholder="Paste the email text - or leave it empty and just attach an SI and a BL." />

        <p className="eyebrow check-docs-title">Documents (optional)</p>
        {fileField("Shipping Instruction (SI)", si, "si")}
        {fileField("Draft Bill of Lading (BL)", bl, "bl")}
        <p className="review-note">
          TXT, PDF, Word, Excel, or a scanned image. Nothing attached = the email is only classified.
        </p>

        <div className="batch-actions">
          <button className="primary-button resolve-button" disabled={!canSubmit} onClick={submit}>
            {busy ? "Checking… (see the live status →)" : "Check"}
          </button>
          {(subject || sender || body || si || bl) && !busy && (
            <button className="link-button" onClick={() => setDraft(EMPTY_DRAFT)}>Clear the form</button>
          )}
        </div>
        {task?.state === "failed" && <p className="run-error">{task.error}</p>}
      </div>

      <div className="panel">
        <p className="eyebrow">Result</p>
        {busy ? (
          <LiveStatus activity={task?.activity} fallback="Uploading…" />
        ) : task?.state === "lost" && !result ? (
          <div className="empty-state">
            <strong>We lost track of this check</strong>
            <span>The server was restarted before it finished. Please press Check again.</span>
          </div>
        ) : !result ? (
          <div className="empty-state">
            <strong>No check yet</strong>
            <span>Fill in the email and/or attach documents, then press Check.</span>
          </div>
        ) : (
          <>
            <h3>{CATEGORY_LABEL[result.category]}</h3>
            <div className="check-status">
              <StatusBadge status={statusLabel(result.category, result.status)} />
              <span className={`basis-tag basis-${result.basis.kind}`}>{result.basis.label}</span>
            </div>
            {result.category === "BL_COMPARISON" && result.status === "OK" && (
              <p><strong>{result.view.fields.length ? "No mismatch detected." : "Nothing to compare yet"}</strong></p>
            )}
            {result.status === "MISMATCH" && (
              <p><strong>Mismatch in {result.defect_fields.map(fieldLabel).join(", ")}</strong></p>
            )}
            {result.status === "NEEDS_REVIEW" && (
              <p>
                <strong>Needs human review — {REVIEW_REASON_LABEL[result.review_reason ?? ""] ?? result.review_reason}</strong>
                <br />
                {result.note}
              </p>
            )}
            {result.category !== "BL_COMPARISON" && (
              <p className="review-note">{explainClassification(result.classification?.method)} Only document-comparison requests go on to the SI / BL check.</p>
            )}
            {result.view.fields.length > 0 && <FieldTable fields={result.view.fields} />}
            <div className="attachment-links">
              <button
                className="primary-button"
                onClick={() => {
                  setDraft(EMPTY_DRAFT);
                  store.clearCheck();
                }}
              >
                Check another email
              </button>
              <button className="secondary-button" onClick={() => onOpenEmail(result.id)}>Open full view</button>
              {result.needs_review && (
                <button className="primary-button" onClick={() => onReview(result.id)}>Open in review</button>
              )}
              <button
                className="secondary-button danger-button"
                onClick={async () => {
                  if (await confirmDelete("Delete this uploaded email and its attachments?", () => api.deleteEmail(result.id))) {
                    store.clearCheck();
                    onChanged();
                  }
                }}
              >
                Delete
              </button>
            </div>
          </>
        )}
      </div>
    </section>
  );
}

const FIELD_ORDER = [
  "shipper",
  "consignee",
  "notify_party",
  "port_of_loading",
  "port_of_discharge",
  "container_count",
  "gross_weight_kg",
];

function HumanReview({
  initialId,
  onChanged,
}: {
  initialId: string | null;
  onChanged: () => void;
}) {
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resolved, setResolved] = useState<EmailDetail | null>(null);

  const loadQueue = useCallback(async () => {
    try {
      const q = await api.queue();
      setQueue(q);
      setSelected((cur) =>
        cur && q.some((i) => i.email_id === cur)
          ? cur
          : initialId && q.some((i) => i.email_id === initialId)
            ? initialId
            : (q[0]?.email_id ?? null),
      );
    } catch (err) {
      setError(errorText(err));
    }
  }, [initialId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    loadQueue();
  }, [loadQueue]);

  const uploadedCount = queue.filter((q) => q.source === "upload").length;

  if (resolved) {
    return (
      <>
        <header className="topbar">
          <div>
            <p className="eyebrow">Human-in-the-Loop</p>
            <h2>Review Queue</h2>
          </div>
          <div className="ai-badge">
            <span className="status-dot"></span>
            Review saved
          </div>
        </header>
        <section className="panel review-success">
          <div className="success-check">✓</div>
          <h3>Case resolved</h3>
          <p>
            {resolved.id} is now{" "}
            <strong>{statusLabel(resolved.category, resolved.status)}</strong>
            {resolved.defect_fields.length
              ? ` (${resolved.defect_fields.map(fieldLabel).join(", ")})`
              : ""}
            . The report has been updated.
          </p>
          <button
            className="primary-button"
            onClick={() => {
              setResolved(null);
              loadQueue();
            }}
          >
            View Review Queue
          </button>
        </section>
      </>
    );
  }

  return (
    <>
      <header className="topbar">
        <div>
          <p className="eyebrow">Human-in-the-Loop</p>
          <h2>Review Queue</h2>
          <p className="page-subtitle">Cases the system wouldn't decide on its own. Check the evidence, then confirm, correct or reclassify.</p>
        </div>
        <div className="review-count">
          {queue.length} case{queue.length === 1 ? "" : "s"} require
          {queue.length === 1 ? "s" : ""} attention
          {uploadedCount > 0 && (
            <small className="review-split">
              {queue.length - uploadedCount} from the inbox · {uploadedCount} from your uploads
            </small>
          )}
        </div>
      </header>

      {error && <div className="api-error">{error}</div>}

      {queue.length === 0 ? (
        <section className="panel empty-state">
          <strong>Nothing to review</strong>
          <span>Every case has been decided.</span>
        </section>
      ) : (
        <section className="review-shell">
          <div className="panel review-list">
            {queue.map((q, i) => (
              <Fragment key={q.email_id}>
              {(i === 0 || (q.source === "upload") !== (queue[i - 1].source === "upload")) && uploadedCount > 0 && (
                <p className="review-group">{q.source === "upload" ? "Your uploads" : "Provided inbox"}</p>
              )}
              <button
                key={q.email_id}
                className={`review-list-item ${selected === q.email_id ? "active" : ""}`}
                onClick={() => setSelected(q.email_id)}
              >
                <strong title={q.email_id}>
                  {q.original_id ?? (q.source === "upload" ? q.subject || "Your upload" : q.email_id)}
                </strong>
                <span>
                  {REVIEW_REASON_LABEL[q.review_reason ?? ""] ?? "Category to confirm"}
                </span>
                {q.source === "upload" && <em className="upload-tag">Uploaded</em>}
                {q.proposed && q.proposed !== "INCOMPLETE" && (
                  <small>Proposal ready</small>
                )}
              </button>
              </Fragment>
            ))}
          </div>
          {selected && (
            <ReviewCase
              key={selected}
              emailId={selected}
              onResolved={(d) => {
                setResolved(d);
                onChanged();
              }}
            />
          )}
        </section>
      )}
    </>
  );
}

function ReviewCase({
  emailId,
  onResolved,
}: {
  emailId: string;
  onResolved: (d: EmailDetail) => void;
}) {
  const { detail, error } = useEmail(emailId);
  const [reviewer, setReviewerRaw] = useState(() => {
    try {
      return localStorage.getItem(REVIEWER_KEY) ?? "";
    } catch {
      return "";
    }
  });
  const setReviewer = (name: string) => {
    setReviewerRaw(name);
    try {
      localStorage.setItem(REVIEWER_KEY, name);
    } catch {
      /* private mode */
    }
  };
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [newCategory, setNewCategory] = useState<BackendCategory | "">("");

  if (error) return <div className="panel empty-state"><strong>Couldn't load this case</strong><span>{error}</span></div>;
  if (!detail) return <Loading text="Loading the case…" />;

  const fields = Object.fromEntries(detail.view.fields.map((f) => [f.field, f]));
  const proposal = detail.proposed_result;
  const canConfirm = proposal && (proposal.status === "OK" || proposal.status === "MISMATCH");
  const isComparison = detail.category === "BL_COMPARISON";
  // flagged only because the category itself was uncertain (e.g. the AI was unavailable)
  const classificationOnly = detail.status !== "NEEDS_REVIEW";
  const acceptSystem = () =>
    override({
      category: detail.category,
      status: detail.status,
      defect_fields: detail.defect_fields,
      note: "Reviewer accepted the system's result",
    });
  const key = (side: "si" | "bl", field: string) => `${side}.${field}`;
  const current = (side: "si" | "bl", field: string) => {
    const k = key(side, field);
    if (k in edits) return edits[k];
    const v = fields[field]?.[side];
    return v === null || v === undefined ? "" : String(v);
  };

  const override = async (decision: Omit<Decision, "action" | "reviewer">) => {
    setSaving(true);
    setSaveError(null);
    try {
      onResolved(await api.decide(emailId, { action: "override", reviewer: reviewer || undefined, ...decision }));
    } catch (err) {
      setSaveError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  const submit = async (action: "confirm" | "correct") => {
    setSaving(true);
    setSaveError(null);
    try {
      const si: Record<string, string> = {};
      const bl: Record<string, string> = {};
      if (action === "correct") {
        for (const f of FIELD_ORDER) {
          si[f] = current("si", f);
          bl[f] = current("bl", f);
        }
      }
      const d = await api.decide(emailId, {
        action,
        reviewer: reviewer || undefined,
        si_fields: action === "correct" ? si : undefined,
        bl_fields: action === "correct" ? bl : undefined,
      });
      onResolved(d);
    } catch (err) {
      setSaveError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  const allFilled = FIELD_ORDER.every(
    (f) => current("si", f).trim() && current("bl", f).trim(),
  );

  return (
    <div className="review-case-grid">
      <div className="panel review-case">
        <div className="review-case-header">
          <div>
            <span className="review-priority">Needs Review</span>
            <h3>{detail.subject}</h3>
            <p>
              {detail.original_id ?? detail.id}
              {detail.source === "upload" ? " (uploaded)" : ""} · {detail.sender || "unknown sender"}
            </p>
          </div>
          <span className={`basis-tag basis-${detail.basis.kind}`}>
            {detail.basis.label}
          </span>
        </div>

        <div className="review-reason">
          <strong>Why was this escalated?</strong>
          <p>
            {classificationOnly
              ? `The category was uncertain. ${explainClassification(detail.classification?.method)}`
              : `${REVIEW_REASON_LABEL[detail.review_reason ?? ""] ?? "Needs a decision"}${detail.note ? ` — ${detail.note}` : ""}`}
          </p>
        </div>

        <div className="source-evidence">
          <p className="eyebrow">Source Evidence</p>
          {detail.documents.map((d) => (
            <div key={d.side} className="evidence-doc">
              <h3>
                {d.side === "SI" ? "Shipping Instruction" : "Bill of Lading"}{" "}
                <small>
                  {d.file_type?.toUpperCase()} · {d.read_method ?? "not read"}
                  {d.ocr ? ` · OCR ${Math.round(d.ocr.confidence * 100)}%` : ""}
                </small>
              </h3>
              {d.error && <p className="run-error">{d.error}</p>}
              {d.warnings.map((w) => (
                <p key={w} className="review-note">⚠ {w}</p>
              ))}
              {d.text_preview && (
                <pre className="document-preview evidence-text">{d.text_preview}</pre>
              )}
            </div>
          ))}
          <AttachmentLinks detail={detail} />
        </div>
      </div>

      <div className="panel review-action-panel">
        <label className="review-label">Reviewer</label>
        <input
          className="review-input"
          placeholder="Your name"
          value={reviewer}
          onChange={(e) => setReviewer(e.target.value)}
        />

        {classificationOnly && (
          <>
            <p className="eyebrow">System result</p>
            <div className="ai-suggestion">
              <span>Please confirm the category</span>
              <strong>
                {CATEGORY_LABEL[detail.category]}
                {isComparison ? ` · ${statusLabel(detail.category, detail.status)}` : ""}
                {detail.defect_fields.length ? ` (${detail.defect_fields.map(fieldLabel).join(", ")})` : ""}
              </strong>
            </div>
            <button className="suggestion-button" disabled={saving} onClick={acceptSystem}>
              Accept the system's result
            </button>
          </>
        )}

        {isComparison && (
        <>
        <p className="eyebrow">{canConfirm ? "Proposed Result" : "Correct the values"}</p>
        {canConfirm && (
          <div className="ai-suggestion">
            <span>System proposal (unconfirmed)</span>
            <strong>
              {proposal!.status === "OK"
                ? "No mismatch detected."
                : `Mismatch: ${proposal!.defect_fields.map(fieldLabel).join(", ")}`}
            </strong>
          </div>
        )}

        <div className="edit-grid">
          <span></span>
          <span className="edit-head">SI</span>
          <span className="edit-head">BL</span>
          {FIELD_ORDER.map((f) => (
            <FieldEditRow
              key={f}
              field={f}
              row={fields[f]}
              si={current("si", f)}
              bl={current("bl", f)}
              onChange={(side, value) =>
                setEdits((e) => ({ ...e, [key(side, f)]: value }))
              }
            />
          ))}
        </div>

        {canConfirm && (
          <button
            className="suggestion-button"
            disabled={saving}
            onClick={() => submit("confirm")}
          >
            Confirm proposal
          </button>
        )}
        <button
          className="primary-button resolve-button"
          disabled={saving || !allFilled}
          onClick={() => submit("correct")}
        >
          Save values & re-check
        </button>
        {saveError && <p className="run-error">{saveError}</p>}
        <p className="review-note">
          Your values replace what the system read; the seven fields are compared
          again and the report is updated.
        </p>
        </>
        )}

        <div className="other-actions">
          <p className="eyebrow">Other outcomes</p>
          {detail.category === "BL_COMPARISON" && detail.review_reason && (
            <button
              className="secondary-button"
              disabled={saving}
              onClick={() =>
                override({
                  status: "NEEDS_REVIEW",
                  review_reason: detail.review_reason ?? undefined,
                  note: "Acknowledged: sender asked for the correct / missing documents",
                })
              }
            >
              Acknowledge — request new documents
            </button>
          )}
          <div className="reclassify-row">
            <select
              className="review-input"
              value={newCategory}
              onChange={(e) => setNewCategory(e.target.value as BackendCategory | "")}
            >
              <option value="">Reclassify email as…</option>
              {(Object.keys(CATEGORY_LABEL) as BackendCategory[])
                .filter((c) => c !== detail.category)
                .map((c) => (
                  <option key={c} value={c}>
                    {CATEGORY_LABEL[c]}
                  </option>
                ))}
            </select>
            <button
              className="secondary-button"
              disabled={saving || !newCategory}
              onClick={() =>
                newCategory &&
                override(
                  newCategory === "BL_COMPARISON"
                    ? { category: newCategory, status: "NEEDS_REVIEW", review_reason: "missing_value" }
                    : { category: newCategory, status: "OK" },
                )
              }
            >
              Save
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function FieldEditRow({
  field,
  row,
  si,
  bl,
  onChange,
}: {
  field: string;
  row: FieldRow | undefined;
  si: string;
  bl: string;
  onChange: (side: "si" | "bl", value: string) => void;
}) {
  const flagged = row && row.match === false;
  return (
    <>
      <span className={`edit-label ${flagged ? "edit-flagged" : ""}`}>
        {row?.label ?? fieldLabel(field)}
      </span>
      <input
        className={`review-input ${!si.trim() ? "input-missing" : ""}`}
        title={si}
        value={si}
        placeholder="missing"
        onChange={(e) => onChange("si", e.target.value)}
      />
      <input
        className={`review-input ${!bl.trim() ? "input-missing" : ""}`}
        title={bl}
        value={bl}
        placeholder="missing"
        onChange={(e) => onChange("bl", e.target.value)}
      />
    </>
  );
}

function StatCard({
  title,
  value,
  description,
}: {
  title: string;
  value: string;
  description: string;
}) {
  return (
    <div className="stat-card">
      <span>{title}</span>
      <strong>{value}</strong>
      <p>{description}</p>
    </div>
  );
}

function StatusBadge({
  status,
}: {
  status: Status;
}) {
  const className = status
    .toLowerCase()
    .replaceAll(" ", "-");

  return (
    <span
      className={`status-badge ${className}`}
    >
      {status}
    </span>
  );
}

function CategoryBadge({
  category,
}: {
  category: Category;
}) {
  const className = category
    .toLowerCase()
    .replaceAll(" ", "-");

  return (
    <span
      className={`category-badge ${className}`}
    >
      {category}
    </span>
  );
}

export default App;
