export function canStartFirstChapter(job: { status: string; active: boolean } | null): boolean {
  return !job?.active && job?.status !== "completed";
}

export function timelineNodeState(state: string | undefined, running: boolean, liveAttempt = true): string {
  return state === "running" && (!running || !liveAttempt) ? "stopped" : state || "info";
}

/** Refresh all cached rows, including records outside the newest paginated window. */
export function refreshLogActivity<T extends { attempt_key?: string; is_live_attempt?: boolean }>(rows: T[], activeKey: string | null | undefined): T[] {
  if (activeKey === undefined) return rows;
  return rows.map((row) => ({ ...row, is_live_attempt: Boolean(activeKey && row.attempt_key === activeKey) }));
}

/** Wall-clock interval, including pauses/retries; never a sum of overlapping node durations. */
export function wallDuration(ms: number | null | undefined): string | null {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  const total = Math.floor(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total / 60) % 60;
  const seconds = String(total % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}` : `${minutes}:${seconds}`;
}
