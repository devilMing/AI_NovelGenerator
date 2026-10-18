"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@heroui/react";
import { useLocale, useTranslations } from "next-intl";
import { apiGet } from "@/lib/api";

interface LogRow {
  id: number; time: string; level: string; source: string; kind: string; message: string;
  metadata: Record<string, string>; truncated: boolean; payload?: string;
}
interface Page { items: LogRow[]; cursor: number; before: number | null; has_more: boolean; dropped: number; storage_error: string | null }
interface Filters { q: string; level: string; category: string; start: string; end: string }
const INITIAL: Filters = { q: "", level: "", category: "all", start: "", end: "" };
const INPUT = "min-h-10 min-w-0 w-full rounded-lg border border-border bg-background px-3 text-sm text-foreground";

/** 实时追踪使用递增游标补取，暂停仅停止刷新，历史记录继续保存在服务端。 */
export function RuntimeLogs({ refreshKey, initialQuery = "" }: { refreshKey: number; initialQuery?: string }) {
  const t = useTranslations("settings.logs");
  const locale = useLocale();
  const [mode, setMode] = useState<"live" | "history">(initialQuery ? "history" : "live");
  const [form, setForm] = useState<Filters>({ ...INITIAL, q: initialQuery });
  const [filters, setFilters] = useState<Filters>({ ...INITIAL, q: initialQuery });
  const [rows, setRows] = useState<LogRow[]>([]);
  const [before, setBefore] = useState<number | null>(null);
  const [paused, setPaused] = useState(false);
  const [follow, setFollow] = useState(true);
  const [error, setError] = useState("");
  const [warning, setWarning] = useState(false);
  const [loading, setLoading] = useState(true);
  const [olderBusy, setOlderBusy] = useState(false);
  const [selected, setSelected] = useState<LogRow | null>(null);
  const [detailError, setDetailError] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);
  const [reload, setReload] = useState(0);
  const cursor = useRef<number | null>(null);
  const request = useRef(0);
  const detailRequest = useRef(0);
  const list = useRef<HTMLDivElement>(null);
  const seenQuery = useRef("");
  const key = JSON.stringify({ mode, filters, refreshKey, reload });
  const params = new URLSearchParams({ category: filters.category, q: filters.q, limit: "100" });
  if (filters.level) params.set("level", filters.level);
  if (mode === "history") {
    if (filters.start) params.set("start", new Date(filters.start).toISOString());
    if (filters.end) params.set("end", new Date(filters.end).toISOString());
  }
  const query = params.toString();

  useEffect(() => {
    const epoch = ++request.current;
    const reset = seenQuery.current !== key;
    if (reset) {
      seenQuery.current = key; cursor.current = null;
      setRows([]); setBefore(null); setSelected(null); setDetailError(""); setLoading(true);
      detailRequest.current += 1;
    }
    if (mode === "live" && paused && !reset) { setLoading(false); return; }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const read = async () => {
      const incremental = mode === "live" && cursor.current !== null;
      let more = false;
      try {
        const page = await apiGet<Page>(`/api/runtime/logs?${query}${incremental ? `&after=${cursor.current}` : ""}`, { signal: controller.signal });
        if (epoch !== request.current) return;
        cursor.current = page.cursor;
        setRows((previous) => {
          const merged = new Map((incremental ? previous : []).map((row) => [row.id, row]));
          page.items.forEach((row) => merged.set(row.id, row));
          return [...merged.values()].sort((a, b) => b.id - a.id).slice(0, 1000);
        });
        if (!incremental) setBefore(page.before);
        setWarning(Boolean(page.storage_error || page.dropped)); setError("");
        more = incremental && page.has_more;
      } catch (failure) { if (epoch === request.current) setError(failure instanceof Error ? failure.message : t("readFailed")); }
      finally {
        if (epoch === request.current) {
          setLoading(false);
          if (mode === "live" && !paused) timer = setTimeout(() => void read(), more ? 50 : 1000);
        }
      }
    };
    void read();
    return () => { controller.abort(); clearTimeout(timer); if (request.current === epoch) request.current += 1; };
  }, [key, mode, paused, query, t]);

  useEffect(() => { if (mode === "live" && follow) list.current?.scrollTo({ top: 0 }); }, [rows, mode, follow]);

  const choose = async (row: LogRow) => {
    const epoch = ++detailRequest.current;
    setSelected(row); setDetailError(""); setDetailLoading(true);
    try {
      const detail = await apiGet<LogRow>(`/api/runtime/logs/${row.id}`);
      if (epoch === detailRequest.current) setSelected(detail);
    } catch (failure) { if (epoch === detailRequest.current) setDetailError(failure instanceof Error ? failure.message : t("readFailed")); }
    finally { if (epoch === detailRequest.current) setDetailLoading(false); }
  };
  const older = async () => {
    if (!before || olderBusy) return;
    const epoch = request.current;
    setOlderBusy(true);
    try {
      const page = await apiGet<Page>(`/api/runtime/logs?${query}&before=${before}`);
      if (epoch !== request.current) return;
      setRows((previous) => {
        const merged = new Map(previous.map((row) => [row.id, row]));
        page.items.forEach((row) => merged.set(row.id, row));
        return [...merged.values()].sort((a, b) => b.id - a.id);
      });
      setBefore(page.before); setError("");
    } catch (failure) { if (epoch === request.current) setError(failure instanceof Error ? failure.message : t("readFailed")); }
    finally { setOlderBusy(false); }
  };
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (form.start && form.end && new Date(form.start) > new Date(form.end)) { setError(t("badRange")); return; }
    setFilters({ ...form, q: form.q.trim() }); setReload((value) => value + 1);
  };
  const label = (kind: string) => t.has(`kinds.${kind}`) ? t(`kinds.${kind}`) : kind;
  const summary = (row: LogRow) => {
    const parts = row.message.split("|");
    return row.kind === "legacy" && parts.length > 5 ? parts.slice(5).join("|").trim() : row.message;
  };

  return <div className="flex h-full min-h-0 min-w-0 flex-col gap-4">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><h2 className="text-lg font-semibold">{t("title")}</h2><p className="mt-1 text-xs leading-5 text-muted">{t("description")}</p></div>
      <div className="flex items-center gap-2">
        {mode === "live" && <Button size="sm" variant="outline" onPress={() => setPaused(!paused)}>{t(paused ? "resume" : "pause")}</Button>}
        <Button size="sm" variant="outline" onPress={() => setReload((value) => value + 1)}>{t("refresh")}</Button>
      </div>
    </div>
    <div className="flex flex-wrap items-center gap-2" aria-label={t("mode")}>
      {(["live", "history"] as const).map((item) => <Button key={item} size="sm" variant={mode === item ? "primary" : "ghost"} aria-pressed={mode === item} onPress={() => setMode(item)}>{t(item)}</Button>)}
      {mode === "live" && <><span role="status" className={`ml-2 text-xs ${error ? "text-danger" : "text-muted"}`}>{t(paused ? "paused" : error ? "reconnecting" : "connected")}</span><label className="ml-auto flex min-h-10 items-center gap-2 text-xs text-muted"><input type="checkbox" checked={follow} onChange={(event) => setFollow(event.target.checked)} />{t("follow")}</label></>}
    </div>
    <form onSubmit={submit} className="flex flex-wrap items-end gap-3 rounded-xl border border-border bg-surface-secondary/30 p-3">
      <label className="grid min-w-32 flex-1 gap-1 text-xs text-muted">{t("category")}<select className={INPUT} value={form.category} onChange={(event) => setForm({ ...form, category: event.target.value })}>{["all", "llm", "system"].map((value) => <option key={value} value={value}>{t(`categories.${value}`)}</option>)}</select></label>
      <label className="grid min-w-28 flex-1 gap-1 text-xs text-muted">{t("level")}<select className={INPUT} value={form.level} onChange={(event) => setForm({ ...form, level: event.target.value })}><option value="">{t("allLevels")}</option>{["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].map((value) => <option key={value}>{value}</option>)}</select></label>
      <label className="grid min-w-48 flex-[2] gap-1 text-xs text-muted">{t("keyword")}<input className={INPUT} value={form.q} maxLength={300} placeholder={t("keywordHint")} onChange={(event) => setForm({ ...form, q: event.target.value })} /></label>
      {mode === "history" && <>{(["start", "end"] as const).map((field) => <label key={field} className="grid min-w-44 flex-1 gap-1 text-xs text-muted">{t(field)}<input type="datetime-local" className={INPUT} value={form[field]} onChange={(event) => setForm({ ...form, [field]: event.target.value })} /></label>)}</>}
      <Button type="submit" size="sm" variant="primary" className="min-h-10">{t("search")}</Button>
    </form>
    {error && <p role="alert" className="text-sm text-danger">{error}</p>}
    {warning && <p role="alert" className="text-xs text-danger">{t("storageWarning")}</p>}
    <div className="grid min-h-80 min-w-0 flex-1 gap-4 lg:min-h-0 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <div className="flex min-h-0 min-w-0 flex-col rounded-xl border border-border">
        <div className="border-b border-border px-3 py-2 text-xs text-muted">{t("count", { count: rows.length })}</div>
        <div ref={list} className="workspace-scrollbar min-h-0 flex-1 overflow-y-auto max-lg:max-h-96">
          {loading && !rows.length && <p role="status" className="p-4 text-sm text-muted">{t("loading")}</p>}
          {!loading && !rows.length && <p className="p-4 text-sm text-muted">{t("empty")}</p>}
          <ul className="divide-y divide-border">{rows.map((row) => <li key={row.id}>
            <button type="button" aria-pressed={selected?.id === row.id} onClick={() => void choose(row)} className={`block w-full min-w-0 space-y-1 px-3 py-3 text-left outline-none hover:bg-surface-secondary focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent ${selected?.id === row.id ? "bg-selected" : ""}`}>
              <span className="flex flex-wrap items-center gap-2 text-xs"><time className="tabular-nums text-muted">{new Date(row.time).toLocaleString(locale)}</time><span className={row.level === "ERROR" || row.level === "CRITICAL" ? "font-semibold text-danger" : "font-medium text-accent"}>{row.level}</span><span className="text-muted">{label(row.kind)}</span></span>
              <span className="block truncate text-sm">{summary(row)}</span>
              <span className="block truncate text-xs text-muted">{row.metadata.model || row.source}{row.metadata.task && ` · ${row.metadata.task}`}{row.metadata.request_id && ` · ${row.metadata.request_id.slice(0, 8)}`}</span>
            </button>
          </li>)}</ul>
          {mode === "history" && before && <Button size="sm" variant="ghost" className="m-2" isDisabled={olderBusy} onPress={() => void older()}>{t(olderBusy ? "loading" : "older")}</Button>}
        </div>
      </div>
      <section aria-label={t("detail")} className="flex min-h-0 min-w-0 flex-col rounded-xl border border-border">
        <h3 className="border-b border-border px-3 py-2 text-sm font-medium">{t("detail")}{selected && ` #${selected.id}`}</h3>
        <div className="workspace-scrollbar min-h-0 flex-1 overflow-y-auto p-3 max-lg:max-h-[36rem]">
          {!selected ? <p className="text-sm text-muted">{t("select")}</p> : <div className="space-y-3">
            <p className="line-clamp-2 break-words text-sm font-medium" title={selected.message}>{summary(selected)}</p>
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs"><dt className="text-muted">{t("time")}</dt><dd>{new Date(selected.time).toLocaleString(locale)}</dd><dt className="text-muted">{t("source")}</dt><dd className="break-all">{selected.source}</dd>{Object.entries(selected.metadata).map(([name, value]) => <div key={name} className="contents"><dt className="text-muted">{name}</dt><dd className="break-all">{String(value)}</dd></div>)}</dl>
            {detailLoading && <p role="status" className="text-xs text-muted">{t("loading")}</p>}
            {detailError && <p role="alert" className="text-sm text-danger">{detailError}</p>}
            {selected.payload && <pre tabIndex={0} className="workspace-scrollbar overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-surface-secondary p-3 text-xs leading-5">{selected.payload}</pre>}
            {selected.truncated && <p className="text-xs text-muted">{t("truncated")}</p>}
          </div>}
        </div>
      </section>
    </div>
    <p className="text-xs leading-5 text-muted">{t("retention")}</p>
  </div>;
}
