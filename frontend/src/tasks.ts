// tasks.ts — background work (inbox uploads, single checks, full runs) lives
// here, at app level, so switching pages never loses it. Tasks are saved in
// sessionStorage, so even a page reload picks them up again. One poller
// follows every running task (light requests, once a second) and stops when
// nothing is running. If the server no longer knows a task (e.g. it
// restarted), the task is marked "lost" with a clear explanation.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, CATEGORY_LABEL, statusLabel } from "./api";
import type { Activity, BatchStatus, EmailDetail } from "./api";

export type TaskKind = "batch" | "check" | "run";
export type TaskState = "running" | "done" | "failed" | "lost";

export type Task = {
  key: string;
  kind: TaskKind;
  id: string;             // batch id / check job id / "run"
  label: string;
  state: TaskState;
  startedAt: number;
  finishedAt?: number;
  done: number;
  total: number;
  activity: Activity | null;
  summary?: string;       // short, friendly outcome
  resultId?: string;      // single check: the email it produced
  error?: string;
  dismissed: boolean;
  orphan?: boolean;       // restored after a reload: its request no longer exists
};

const STORE = "sdoc_tasks_v1";
const VIEW_STORE = "sdoc_view_v1";
const MAX_TASKS = 12;

function load<T>(key: string, fallback: T): T {
  try {
    const raw = sessionStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function save(key: string, value: unknown) {
  try {
    sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage full / private mode: state still works in memory */
  }
}

function plural(n: number, word: string, many = `${word}s`) {
  return `${n} ${n === 1 ? word : many}`;
}

export function batchSummary(b: BatchStatus): string {
  const c = b.counts ?? {};
  const mism = c["BL_COMPARISON/MISMATCH"] ?? 0;
  const review = c["BL_COMPARISON/NEEDS_REVIEW"] ?? 0;
  const parts = [`All done: ${plural(b.total, "email")} checked`];
  const extra = [mism ? plural(mism, "mismatch", "mismatches") : "", review ? `${review} for review` : ""]
    .filter(Boolean)
    .join(", ");
  return extra ? `${parts[0]} · ${extra}` : parts[0];
}

function checkSummary(d: EmailDetail): string {
  const cat = CATEGORY_LABEL[d.category];
  if (d.category !== "BL_COMPARISON") return `Done: classified as ${cat}`;
  if (d.status === "MISMATCH") return `Done: mismatch in ${d.defect_fields.length} field${d.defect_fields.length === 1 ? "" : "s"}`;
  return `Done: ${statusLabel(d.category, d.status)}`;
}

type View = { batchId: string | null; checkResultId: string | null };

export function useTasks(onDataChanged: () => void) {
  const [tasks, setTasks] = useState<Task[]>(() =>
    load<Task[]>(STORE, []).map((t) => ({
      ...t,
      activity: null,
      orphan: t.state === "running" && t.kind === "check" ? true : t.orphan,
    })),
  );
  const [view, setView] = useState<View>(() => load<View>(VIEW_STORE, { batchId: null, checkResultId: null }));
  const [batchView, setBatchView] = useState<BatchStatus | null>(null);
  const [checkResult, setCheckResult] = useState<EmailDetail | null>(null);
  const tasksRef = useRef(tasks);
  tasksRef.current = tasks;
  const changedRef = useRef(onDataChanged);
  changedRef.current = onDataChanged;

  useEffect(() => save(STORE, tasks.map((t) => ({ ...t, activity: null }))), [tasks]);
  useEffect(() => save(VIEW_STORE, view), [view]);

  const update = useCallback((key: string, patch: Partial<Task>) => {
    setTasks((ts) => ts.map((t) => (t.key === key ? { ...t, ...patch } : t)));
  }, []);

  const add = useCallback((task: Task) => {
    setTasks((ts) => [...ts.filter((t) => t.key !== task.key), task].slice(-MAX_TASKS));
  }, []);

  // ---------------------------------------------------------------- restore views after a reload
  useEffect(() => {
    if (view.batchId) {
      api.batch(view.batchId).then(setBatchView).catch(() => setBatchView(null));
    }
    if (view.checkResultId) {
      api.email(view.checkResultId).then(setCheckResult).catch(() => setCheckResult(null));
    }
    // run once on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---------------------------------------------------------------- actions
  const startBatch = useCallback(async (files: File[]) => {
    const started = await api.startBatch(files);
    const key = `batch:${started.id}`;
    add({
      key, kind: "batch", id: started.id, label: `Inbox upload (${plural(started.total, "email")})`,
      state: "running", startedAt: Date.now(), done: started.done, total: started.total,
      activity: null, dismissed: false,
    });
    setView((v) => ({ ...v, batchId: started.id }));
    setBatchView(null);
    return started;
  }, [add]);

  const openBatch = useCallback(async (id: string) => {
    setView((v) => ({ ...v, batchId: id }));
    try {
      setBatchView(await api.batch(id));
    } catch {
      setBatchView(null);
    }
  }, []);

  const closeBatch = useCallback(() => {
    setView((v) => ({ ...v, batchId: null }));
    setBatchView(null);
  }, []);

  const startCheck = useCallback(async (form: FormData, jobId: string, label: string) => {
    const key = `check:${jobId}`;
    add({
      key, kind: "check", id: jobId, label, state: "running", startedAt: Date.now(),
      done: 0, total: 1, activity: null, dismissed: false,
    });
    setCheckResult(null);
    setView((v) => ({ ...v, checkResultId: null }));
    try {
      const detail = await api.check(form);
      update(key, { state: "done", finishedAt: Date.now(), done: 1, resultId: detail.id, summary: checkSummary(detail) });
      setCheckResult(detail);
      setView((v) => ({ ...v, checkResultId: detail.id }));
      changedRef.current();
    } catch (err) {
      update(key, { state: "failed", finishedAt: Date.now(), error: err instanceof Error ? err.message : String(err) });
    }
  }, [add, update]);

  const clearCheck = useCallback(() => {
    setCheckResult(null);
    setView((v) => ({ ...v, checkResultId: null }));
  }, []);

  const trackRun = useCallback((label: string) => {
    add({
      key: "run:run", kind: "run", id: "run", label, state: "running", startedAt: Date.now(),
      done: 0, total: 0, activity: null, dismissed: false,
    });
  }, [add]);

  const dismiss = useCallback((key: string) => update(key, { dismissed: true }), [update]);

  // ---------------------------------------------------------------- one poller for everything running
  const anyRunning = tasks.some((t) => t.state === "running");
  useEffect(() => {
    if (!anyRunning) return;
    let busy = false;
    const misses: Record<string, number> = {};
    const tick = async () => {
      if (busy) return;
      busy = true;
      try {
        for (const t of tasksRef.current.filter((x) => x.state === "running")) {
          try {
            if (t.kind === "batch") {
              const s = await api.batch(t.id, false);
              if (s.running) {
                update(t.key, { done: s.done, total: s.total, activity: s.activity ?? null });
              } else {
                const full = await api.batch(t.id);
                update(t.key, {
                  state: full.error ? "failed" : "done", finishedAt: Date.now(), done: full.total,
                  total: full.total, activity: null, error: full.error ?? undefined,
                  summary: full.error ? undefined : batchSummary(full),
                });
                setBatchView((cur) => (cur === null || cur.id === t.id ? full : cur));
                changedRef.current();
              }
            } else if (t.kind === "check") {
              const a = await api.activity(t.id);
              const job = a.job;
              if (job) {
                misses[t.key] = 0;
                update(t.key, { activity: job.running ? job : null });
                // restored after a reload: the original request is gone, so finish from the job itself
                if (t.orphan && !job.running) {
                  if (job.error || !job.target) {
                    update(t.key, { state: "failed", finishedAt: Date.now(), error: job.error ?? "The check did not finish" });
                  } else {
                    const d = await api.email(job.target);
                    update(t.key, { state: "done", finishedAt: Date.now(), done: 1, resultId: d.id, summary: checkSummary(d) });
                    setCheckResult(d);
                    setView((v) => ({ ...v, checkResultId: d.id }));
                    changedRef.current();
                  }
                }
              } else if (t.orphan && (misses[t.key] = (misses[t.key] ?? 0) + 1) >= 3) {
                update(t.key, { state: "lost", finishedAt: Date.now() });
              }
            } else if (t.kind === "run") {
              const r = await api.runStatus();
              if (r.running) {
                update(t.key, { done: r.done, total: r.total, activity: r.activity ?? null });
              } else {
                update(t.key, {
                  state: r.error ? "failed" : "done", finishedAt: Date.now(), activity: null,
                  error: r.error ?? undefined, summary: r.error ? undefined : "The provided inbox was processed again",
                });
                changedRef.current();
              }
            }
          } catch (err) {
            if (err instanceof ApiError && err.status === 404) {
              update(t.key, { state: "lost", finishedAt: Date.now(), activity: null });
            }
            // other errors (network blip): keep trying on the next tick
          }
        }
      } finally {
        busy = false;
      }
    };
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [anyRunning, update]);

  return {
    tasks, dismiss,
    batchId: view.batchId, batchView, startBatch, openBatch, closeBatch,
    checkResult, startCheck, clearCheck,
    trackRun,
  };
}

export type TaskStore = ReturnType<typeof useTasks>;
