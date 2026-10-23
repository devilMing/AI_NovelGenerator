/** Running nodes have no final duration; zero is commonly a start-event placeholder. */
export function completedLogElapsedSeconds(row: { state: string; elapsed_ms?: number }): string | null {
  if (row.state !== "done" && row.state !== "failed") return null;
  if (typeof row.elapsed_ms !== "number" || !Number.isFinite(row.elapsed_ms) || row.elapsed_ms < 0) return null;
  return (row.elapsed_ms / 1000).toFixed(2);
}
