"use client";

import { useEffect, useId, useState } from "react";
import { Button } from "@heroui/react";
import { useLocale, useTranslations } from "next-intl";
import { apiGet } from "@/lib/api";
import { completedLogElapsedSeconds } from "@/lib/writingLogElapsed";
import { refreshLogActivity, timelineNodeState } from "@/lib/firstChapterTimeline";

interface LogRow {
  id: string; stage: string; title: string; label?: string; state: string;
  elapsed_ms?: number; message?: string; output?: string; output_truncated?: boolean;
  updated_at?: string; round?: number; call_count?: number; tool_name?: string;
  network_state?: string; retry_attempt?: number; retry_limit?: number; retry_after?: number;
  is_live_attempt?: boolean;
  attempt_key?: string;
}
interface LogPage { items: LogRow[]; before: string | null; legacy: boolean; live_attempt_key?: string | null }

/** 按需读取持久化节点日志；关闭面板不影响后台任务。 */
export default function VolumeWritingLog({ base, jobId, running, stepId, defaultExpanded = false }: { base: string; jobId: string; running: boolean; stepId?: string; defaultExpanded?: boolean }) {
  const t = useTranslations("writing.volumeWriting.logs");
  const flow = useTranslations("taskFlows");
  const stage = useTranslations("writing.volumeWriting.stage");
  const locale = useLocale();
  const detailsId = useId();
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [rows, setRows] = useState<LogRow[]>([]);
  const [before, setBefore] = useState<string | null>(null);
  const [legacy, setLegacy] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [olderLoading, setOlderLoading] = useState(false);
  const url = `${base}/${jobId}/logs${stepId ? `?step_id=${encodeURIComponent(stepId)}` : ""}`;

  useEffect(() => {
    if (!expanded) return;
    const controller = new AbortController();
    let active = true;
    let initial = true;
    let timer: ReturnType<typeof setTimeout>;
    const read = async () => {
      try {
        const page = await apiGet<LogPage>(url, { signal: controller.signal });
        if (!active) return;
        const replace = initial;
        setRows((previous) => {
          const byId = new Map((replace || page.legacy ? [] : previous.filter((row) => !row.id.startsWith("legacy-"))).map((row) => [row.id, row]));
          page.items.forEach((row) => byId.set(row.id, row));
          return refreshLogActivity([...byId.values()], page.live_attempt_key).sort((a, b) => a.id.localeCompare(b.id));
        });
        if (initial) setBefore(page.before);
        initial = false;
        setLegacy(page.legacy); setError("");
      } catch (failure) { if (active) setError(failure instanceof Error ? failure.message : t("readFailed")); }
      finally { if (active) { setLoading(false); if (running) timer = setTimeout(() => void read(), 2000); } }
    };
    setLoading(true); void read();
    return () => { active = false; controller.abort(); clearTimeout(timer); };
  }, [expanded, url, t, running]);

  const older = async () => {
    if (!before || olderLoading) return;
    setOlderLoading(true);
    try {
      const page = await apiGet<LogPage>(`${url}${stepId ? "&" : "?"}before=${before}`);
      setRows((previous) => {
        const byId = new Map(page.items.map((row) => [row.id, row]));
        previous.forEach((row) => byId.set(row.id, row));
        return refreshLogActivity([...byId.values()], page.live_attempt_key).sort((a, b) => a.id.localeCompare(b.id));
      });
      setBefore(page.before); setError("");
    } catch (failure) { setError(failure instanceof Error ? failure.message : t("readFailed")); }
    finally { setOlderLoading(false); }
  };

  return <section className="min-w-0 border-y border-border" aria-label={t("title")}>
    <div className="flex min-h-11 items-center justify-between gap-3">
      <span className="text-sm font-medium">{t("title")}</span>
      <Button size="sm" variant="ghost" aria-expanded={expanded} aria-controls={detailsId} onPress={() => setExpanded(!expanded)}>{t(expanded ? "hide" : "show")}</Button>
    </div>
    <div id={detailsId} hidden={!expanded}>
      {expanded && <>
        <p className="pb-2 text-xs leading-5 text-muted">{t(legacy ? "legacy" : running ? "hint" : "snapshotHint")}</p>
        {error && <p role="alert" className="py-2 text-sm text-danger">{error}</p>}
        {loading && !rows.length && <p role="status" className="py-3 text-sm text-muted">{t("loading")}</p>}
        {!loading && !rows.length && !error && <p className="py-3 text-sm text-muted">{t("empty")}</p>}
        <ul className="workspace-scrollbar max-h-80 overflow-y-auto border-t border-border divide-y divide-border">
          {[...rows].reverse().map((row) => {
            const state = timelineNodeState(row.state, running, row.is_live_attempt);
            const elapsedSeconds = completedLogElapsedSeconds(row);
            const label = row.label && flow.has(`stepNames.${row.label}`) ? flow(`stepNames.${row.label}`) : row.label || stage(row.stage);
            return <li key={row.id} className="min-w-0 text-sm">
              <details>
                <summary className="flex min-h-11 cursor-pointer items-start justify-between gap-3 py-3 focus-visible:outline-2 focus-visible:outline-accent">
                  <span className="min-w-0 space-y-1"><span className="block break-words font-medium">{label}</span><span className="block break-words text-xs text-muted">{stage(row.stage)} · {row.title}</span></span>
                  <span className={`shrink-0 pt-0.5 text-xs ${state === "failed" ? "text-danger" : state === "running" ? "text-accent" : "text-muted"}`}>
                    {row.network_state === "retrying" && state === "running" ? flow("agent.reconnecting", { attempt: row.retry_attempt ?? 0, limit: row.retry_limit ?? 0, seconds: Math.ceil(row.retry_after ?? 0) }) : state === "info" ? t("infoState") : flow.has(`states.${state}`) ? flow(`states.${state}`) : state}
                  </span>
                </summary>
                <div className="space-y-2 pb-3">
                  {row.updated_at && <p className="text-xs tabular-nums text-muted">{new Date(row.updated_at).toLocaleString(locale)}</p>}
                  {elapsedSeconds !== null && <p className="text-xs text-muted">{flow("elapsed", { seconds: elapsedSeconds })}</p>}
                  {row.message && <p className="whitespace-pre-wrap break-words text-sm">{row.message}</p>}
                  {typeof row.round === "number" && <p className="text-xs text-muted">{flow("agent.round", { round: row.round, count: row.call_count ?? 0 })}</p>}
                  {row.tool_name && <p className="text-xs text-muted">{flow.has(`agent.toolNames.${row.tool_name}`) ? flow(`agent.toolNames.${row.tool_name}`) : row.tool_name}</p>}
                  {row.output && <details><summary className="min-h-11 cursor-pointer py-2 text-xs text-accent">{flow("agent.rawOutput")}</summary><pre tabIndex={0} aria-label={flow("outputs")} className="workspace-scrollbar max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-surface-secondary p-3 text-xs">{row.output}</pre>{row.output_truncated && <p className="mt-2 text-xs text-muted">{t("truncated")}</p>}</details>}
                </div>
              </details>
            </li>;
          })}
        </ul>
        {before && <Button size="sm" variant="ghost" className="my-2" isDisabled={olderLoading} onPress={() => void older()}>{t(olderLoading ? "loading" : "older")}</Button>}
      </>}
    </div>
  </section>;
}
