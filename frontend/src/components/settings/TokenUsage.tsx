"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@heroui/react";
import { useLocale, useTranslations } from "next-intl";
import { apiGet } from "@/lib/api";

type Counts = { requests: number; pending: number; confirmed: number; unknown: number; partial: number; input_tokens: number; output_tokens: number; total_tokens: number; cached_tokens: number; reasoning_tokens: number; cached_reported: number; reasoning_reported: number };
type Row = { id: number; time: string; call_id: string; sequence: number; provider: string; model: string; task: string; novel_id: string; scope: Record<string, string>; status: string; origin: string; coverage: string; total_source?: string; input_tokens: number | null; output_tokens: number | null; total_tokens: number | null; cached_tokens: number | null; reasoning_tokens: number | null };
type Option = { value: string; label: string; sample: { scope?: Record<string, string> } };
type Report = { summary: Counts; groups: (Counts & { label: string; name: string })[]; trend: (Counts & { period: string })[]; chart_limited: boolean; items: Row[]; before: number | null; options: Record<string, Option[]>; started_at: string; oldest_at: string | null; history: { imported_requests?: number; unresolved_calls?: number }; dropped: number; storage_error: string | null };
type Filters = { range: string; start: string; end: string; provider: string; model: string; novel_id: string; volume_id: string; task: string; status: string };
const INITIAL: Filters = { range: "today", start: "", end: "", provider: "", model: "", novel_id: "", volume_id: "", task: "", status: "" };
const INPUT = "min-h-10 w-full min-w-0 rounded-lg border border-border bg-background px-3 text-sm text-foreground";

/** Actual provider counters are polled independently from log retention and config saving. */
export function TokenUsage({ refreshKey, onOpenLogs }: { refreshKey: number; onOpenLogs: (callId: string) => void }) {
  const t = useTranslations("settings.usage");
  const locale = useLocale();
  const format = new Intl.NumberFormat(locale);
  const [form, setForm] = useState<Filters>(INITIAL);
  const [filters, setFilters] = useState<Filters>(INITIAL);
  const [group, setGroup] = useState("model");
  const [paused, setPaused] = useState(false);
  const [report, setReport] = useState<Report | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [before, setBefore] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [olderBusy, setOlderBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const [updated, setUpdated] = useState("");
  const epoch = useRef(0);
  const query = new URLSearchParams({ group, bucket: filters.range === "today" ? "hour" : "day", offset_minutes: String(-new Date().getTimezoneOffset()) });
  for (const key of ["provider", "model", "novel_id", "volume_id", "task", "status"] as const) if (filters[key]) query.set(key, filters[key]);
  // A local midnight is recomputed on each refresh so an open panel also rolls over to a new day.
  if (["today", "7", "30"].includes(filters.range)) {
    const start = new Date(); start.setHours(0, 0, 0, 0);
    if (filters.range !== "today") start.setDate(start.getDate() - Number(filters.range) + 1);
    query.set("start", start.toISOString());
  } else if (filters.range === "custom") {
    if (filters.start) query.set("start", new Date(filters.start).toISOString());
    if (filters.end) query.set("end", new Date(filters.end).toISOString());
  }
  const queryString = query.toString();

  useEffect(() => {
    ++epoch.current;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const invalidate = () => { epoch.current++; };
    const read = async (initial: boolean) => {
      try {
        const result = await apiGet<Report>(`/api/runtime/token-usage?${queryString}`, { signal: controller.signal });
        if (disposed) return;
        setReport(result); setRows(result.items); setBefore(result.before); setError(""); setUpdated(new Date().toLocaleTimeString(locale));
      } catch (failure) { if (!disposed) setError(failure instanceof Error ? failure.message : t("readFailed")); }
      finally {
        if (!disposed) {
          if (initial) setLoading(false);
          if (!paused) timer = setTimeout(() => void read(false), 2000);
        }
      }
    };
    setLoading(true); setOlderBusy(false); setRows([]); setBefore(null); setReport(null);
    void read(true);
    return () => { disposed = true; controller.abort(); clearTimeout(timer); invalidate(); };
  }, [queryString, refreshKey, reload, paused, locale, t]);

  const older = async () => {
    if (!before || olderBusy) return;
    const generation = epoch.current;
    setOlderBusy(true);
    try {
      const result = await apiGet<Report>(`/api/runtime/token-usage?${queryString}&before=${before}`);
      if (generation !== epoch.current) return;
      setRows((previous) => [...new Map([...previous, ...result.items].map((row) => [row.id, row])).values()]);
      setBefore(result.before);
    } catch (failure) { if (generation === epoch.current) setError(failure instanceof Error ? failure.message : t("readFailed")); }
    finally { if (generation === epoch.current) setOlderBusy(false); }
  };
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (form.range === "custom" && (!form.start || !form.end || new Date(form.start) >= new Date(form.end))) { setError(t("badRange")); return; }
    setFilters({ ...form }); setReload((value) => value + 1);
  };
  const count = (value: number | null) => value == null ? t("notReturned") : format.format(value);
  const time = (value: string) => new Date(value).toLocaleString(locale, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const optionName = (key: string, option: Option) => option.sample.scope?.[({ novel_id: "novel_title", volume_id: "volume_title", task: "task_label" } as Record<string, string>)[key]] || option.label || t("unassigned");
  const summary = report?.summary;
  const maxBar = Math.max(1, ...(report?.trend.map((item) => item.input_tokens + item.output_tokens) || []));

  return <div className="workspace-scrollbar h-full min-h-0 overflow-y-auto pr-1">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-lg font-semibold">{t("title")}</h2><p className="mt-1 text-xs leading-5 text-muted">{t("description")}</p></div>
      <div className="flex items-center gap-2"><span role="status" className="text-xs text-muted">{t(paused ? "paused" : error ? "reconnecting" : "live")}</span><Button size="sm" variant="outline" onPress={() => setPaused(!paused)}>{t(paused ? "resume" : "pause")}</Button><Button size="sm" variant="outline" onPress={() => setReload((value) => value + 1)}>{t("refresh")}</Button></div>
    </div>
    <form onSubmit={submit} className="mt-4 grid grid-cols-2 items-end gap-3 rounded-xl border border-border bg-surface-secondary/30 p-3 xl:grid-cols-4">
      <label className="grid gap-1 text-xs text-muted">{t("range")}<select className={INPUT} value={form.range} onChange={(event) => setForm({ ...form, range: event.target.value })}>{["today", "7", "30", "all", "custom"].map((value) => <option value={value} key={value}>{t(`ranges.${value}`)}</option>)}</select></label>
      {(["novel_id", "volume_id", "provider", "model", "task"] as const).map((key) => <label key={key} className="grid gap-1 text-xs text-muted">{t(`filters.${key}`)}<select className={INPUT} value={form[key]} onChange={(event) => setForm({ ...form, [key]: event.target.value })}><option value="">{t("all")}</option>{report?.options[key]?.map((option) => <option key={option.value} value={option.value}>{optionName(key, option)}</option>)}</select></label>)}
      <label className="grid gap-1 text-xs text-muted">{t("status")}<select className={INPUT} value={form.status} onChange={(event) => setForm({ ...form, status: event.target.value })}><option value="">{t("all")}</option>{["pending", "completed", "failed", "cancelled", "interrupted"].map((value) => <option key={value} value={value}>{t(`statuses.${value}`)}</option>)}</select></label>
      {form.range === "custom" && (["start", "end"] as const).map((key) => <label key={key} className="grid gap-1 text-xs text-muted">{t(key)}<input className={INPUT} type="datetime-local" value={form[key]} onChange={(event) => setForm({ ...form, [key]: event.target.value })} /></label>)}
      <Button type="submit" size="sm" variant="primary" className="min-h-10">{t("search")}</Button>
    </form>
    {error && <p role="alert" className="mt-3 text-sm text-danger">{error}</p>}
    {(report?.storage_error || !!report?.dropped) && <p role="alert" className="mt-3 text-sm text-danger">{t("storageWarning", { count: report?.dropped || 0 })}</p>}
    {loading && <p role="status" className="py-8 text-center text-sm text-muted">{t("loading")}</p>}
    {summary && <>
      <div className="mt-4 grid grid-cols-2 gap-3 xl:grid-cols-4">
        {(["total_tokens", "input_tokens", "output_tokens", "requests"] as const).map((key, index) => <div key={key} className={`rounded-xl border p-4 ${index === 0 ? "border-accent/30 bg-accent/10" : "border-border bg-surface"}`}><p className="text-xs text-muted">{t(`cards.${key}`)}</p><p className="mt-2 break-all font-mono text-2xl font-semibold tracking-tight tabular-nums">{format.format(summary[key])}</p><p className="mt-1 text-xs text-muted">{t(key === "requests" ? "actualRequests" : "tokenUnit")}</p></div>)}
      </div>
      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs leading-6 text-muted" aria-live="polite"><span>{t("coverage", { confirmed: summary.confirmed || 0, pending: summary.pending || 0, unknown: summary.unknown || 0, partial: summary.partial || 0 })}</span><span>{t("cache")}: {summary.cached_reported ? format.format(summary.cached_tokens) : t("notProvided")}</span><span>{t("reasoning")}: {summary.reasoning_reported ? format.format(summary.reasoning_tokens) : t("notProvided")}</span><span>{t("updated", { time: updated })}</span></div>
      <div className="mt-4 rounded-xl border border-border p-4">
        <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-sm font-semibold">{t("trend")}</h3><div className="flex gap-4 text-xs text-muted"><span><i className="mr-1.5 inline-block h-2 w-2 rounded-sm bg-accent" />{t("cards.input_tokens")}</span><span><i className="mr-1.5 inline-block h-2 w-2 rounded-sm bg-muted/50" />{t("cards.output_tokens")}</span></div></div>
        {report.trend.length ? <div className="workspace-scrollbar mt-4 overflow-x-auto"><div className="flex h-44 min-w-full items-end gap-2 border-b border-border pb-6" role="img" aria-label={t("trend")}>
          {report.trend.map((point) => <div key={point.period} className="relative flex h-full min-w-9 flex-1 flex-col justify-end" title={`${point.period} · ${t("cards.input_tokens")}: ${format.format(point.input_tokens)} · ${t("cards.output_tokens")}: ${format.format(point.output_tokens)}`}><div className="min-h-0 rounded-t-sm bg-muted/50" style={{ height: `${point.output_tokens / maxBar * 100}%` }} /><div className="min-h-0 bg-accent" style={{ height: `${point.input_tokens / maxBar * 100}%` }} /><span className="absolute -bottom-5 left-1/2 -translate-x-1/2 whitespace-nowrap font-mono text-[10px] text-muted">{point.period.length > 10 ? `${point.period.slice(-2)}h` : point.period.slice(5)}</span></div>)}
        </div></div> : <p className="py-8 text-center text-sm text-muted">{t("empty")}</p>}
        {report.chart_limited && <p className="mt-2 text-xs text-muted">{t("chartLimited")}</p>}
      </div>
      <div className="mt-4 rounded-xl border border-border p-4">
        <div className="flex items-center justify-between gap-3"><h3 className="text-sm font-semibold">{t("breakdown")}</h3><select aria-label={t("group")} className={`${INPUT} max-w-40`} value={group} onChange={(event) => setGroup(event.target.value)}>{["model", "provider", "novel", "volume", "task"].map((key) => <option key={key} value={key}>{t(`groups.${key}`)}</option>)}</select></div>
        <div className="workspace-scrollbar mt-3 max-h-64 overflow-auto"><table className="w-full text-left text-xs"><thead className="text-muted"><tr>{["group", "cards.input_tokens", "cards.output_tokens", "cards.total_tokens", "cards.requests"].map((key) => <th key={key} className="whitespace-nowrap px-2 py-2 font-normal">{t(key)}</th>)}</tr></thead><tbody>{report.groups.map((item) => <tr key={item.label} className="border-t border-border"><td className="max-w-72 px-2 py-3"><span className="line-clamp-2 break-all" title={item.label}>{item.name || t("unassigned")}</span></td>{(["input_tokens", "output_tokens", "total_tokens", "requests"] as const).map((key) => <td key={key} className="px-2 py-3 font-mono tabular-nums">{format.format(item[key])}</td>)}</tr>)}</tbody></table>{!report.groups.length && <p className="py-6 text-center text-sm text-muted">{t("empty")}</p>}</div>
      </div>
      <div className="mt-4 rounded-xl border border-border p-4">
        <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-sm font-semibold">{t("details")}</h3><span className="text-xs text-muted">{t(paused ? "historyPaging" : "livePaging")}</span></div>
        <div className="workspace-scrollbar mt-3 overflow-x-auto"><table className="w-full text-left text-xs"><thead className="text-muted"><tr>{["time", "position", "step", "filters.model", "cards.input_tokens", "cards.output_tokens", "cards.total_tokens", "cache", "reasoning", "status", "logs"].map((key) => <th key={key} className="whitespace-nowrap px-2 py-2 font-normal">{t(key)}</th>)}</tr></thead>
          <tbody>{rows.map((row) => <tr key={row.id} className="border-t border-border align-top"><td className="whitespace-nowrap px-2 py-3 font-mono text-muted">{time(row.time)}<p className="mt-1 text-[10px]">{row.origin === "history" ? t("historyImport") : `#${row.id} · ${t("send", { sequence: row.sequence })}`}</p></td><td className="min-w-32 max-w-48 px-2 py-3"><p className="line-clamp-2">{row.scope.novel_title || row.novel_id || t("unassigned")}</p><p className="mt-1 line-clamp-2 text-muted">{[row.scope.volume_title, row.scope.chapter_title || row.scope.scene_title].filter(Boolean).join(" / ")}</p></td><td className="min-w-28 max-w-48 px-2 py-3"><p>{row.scope.task_label || row.task || t("unassigned")}</p><p className="mt-1 text-muted">{row.scope.node_label || row.scope.node_id}{row.scope.stage && ` / ${row.scope.stage}`}</p></td><td className="min-w-32 max-w-48 break-all px-2 py-3">{row.model}<p className="mt-1 text-muted">{row.provider}</p></td>{(["input_tokens", "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"] as const).map((key) => <td key={key} className="whitespace-nowrap px-2 py-3 font-mono tabular-nums" title={key === "total_tokens" && row.total_source === "sum" ? t("sumTotal") : undefined}>{count(row[key])}</td>)}<td className="whitespace-nowrap px-2 py-3"><span className={row.status === "failed" || row.status === "interrupted" ? "text-danger" : row.status === "pending" ? "text-accent" : "text-muted"}>{t(`statuses.${row.status}`)}</span>{row.coverage === "partial" && <p className="mt-1 text-muted">{t("partial")}</p>}</td><td className="px-2 py-2"><Button size="sm" variant="ghost" onPress={() => onOpenLogs(row.call_id)}>{t("logs")}</Button></td></tr>)}</tbody></table>
          {!rows.length && <p className="py-8 text-center text-sm text-muted">{t("empty")}</p>}
        </div>
        {before && paused && <Button className="mt-3" size="sm" variant="outline" isPending={olderBusy} onPress={() => void older()}>{t("older")}</Button>}
      </div>
      <details className="mb-4 mt-4 rounded-xl border border-border p-4 text-xs leading-6 text-muted"><summary className="cursor-pointer font-medium text-foreground">{t("method")}</summary><p className="mt-2">{t("methodText")}</p><p>{t("subsetNote")}</p><p>{t("quotaNote")}</p><p>{t("historyNote", { count: report.history.imported_requests || 0, unresolved: report.history.unresolved_calls || 0 })}</p><p>{t("startedAt", { time: new Date(report.started_at).toLocaleString(locale) })}</p>{report.oldest_at && <p>{t("oldestAt", { time: new Date(report.oldest_at).toLocaleString(locale) })}</p>}</details>
    </>}
  </div>;
}
