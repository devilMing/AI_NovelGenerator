"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Button } from "@heroui/react";
import { useLocale, useTranslations } from "next-intl";
import { apiGet } from "@/lib/api";
import { notifyResourceChange } from "@/lib/resourceChanges";
import type { VolumeWritingJob } from "@/types/volumeWriting";
import VolumeWriting from "./VolumeWriting";

interface Volume { id: string; title: string }
interface StatusContext {
  openVolume: (volume: Volume) => void;
  revision: number;
  job: VolumeWritingJob | null;
  error: string;
  openDetails: () => void;
}
const Context = createContext<StatusContext>({ openVolume: () => {}, revision: 0, job: null, error: "", openDetails: () => {} });

export function useVolumeWriting() { return useContext(Context); }

/** 小说级状态位于工作区缓存外；关闭弹框和切换篇章不停止状态读取。 */
export function VolumeWritingProvider({ novelId, active, dirty, children }: {
  novelId?: string; active: boolean; dirty: boolean; children: ReactNode;
}) {
  const t = useTranslations("writing.volumeWriting");
  const [job, setJob] = useState<VolumeWritingJob | null>(null);
  const [error, setError] = useState("");
  const [dialog, setDialog] = useState<Volume | null>(null);
  const [revision, setRevision] = useState(0);
  const savedSignature = useRef("");
  const modalUpdates = useRef(0);
  const jobRef = useRef<VolumeWritingJob | null>(null);
  const accept = useCallback((next: VolumeWritingJob | null) => {
    const previous = jobRef.current;
    // 较慢的请求不能覆盖刚收到的同一任务新状态。
    if (previous && next?.id === previous.id && Date.parse(next.updated_at) < Date.parse(previous.updated_at)) return;
    jobRef.current = next; setJob(next); setError("");
    if (!next) { savedSignature.current = ""; return; }
    const signature = `${next.id}:${next.created_chapters}:${next.written_chapters}:${next.status}`;
    if (savedSignature.current && savedSignature.current !== signature) {
      setRevision((value) => value + 1);
      notifyResourceChange({ novelId, source: "manual" });
    }
    savedSignature.current = signature;
  }, [novelId]);

  useEffect(() => {
    if (!novelId || !active) return;
    let current = true;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const read = async () => {
      const observed = modalUpdates.current;
      try {
        const result = await apiGet<{ job: VolumeWritingJob | null }>(`/api/novels/${novelId}/volume-writing-job`, { signal: controller.signal });
        if (current && observed === modalUpdates.current) accept(result.job);
      } catch (failure) {
        if (current && observed === modalUpdates.current) setError(failure instanceof Error ? failure.message : t("readFailed"));
      } finally { if (current) timer = setTimeout(() => void read(), 2000); }
    };
    void read();
    return () => { current = false; controller.abort(); clearTimeout(timer); };
  }, [novelId, active, accept, t]);

  const openVolume = useCallback((volume: Volume) => setDialog(volume), []);
  const openDetails = useCallback(() => {
    const current = jobRef.current;
    if (current) setDialog({ id: current.volume_id, title: current.volume_title || t("title") });
  }, [t]);
  const report = useCallback((next: VolumeWritingJob | null) => {
    // 打开另一卷的开始面板时，不能把正在执行的其他卷任务从状态栏清掉。
    const previous = jobRef.current;
    if (previous?.active && next?.id !== previous.id) return;
    if (!next || !next.active && previous && Date.parse(next.created_at) < Date.parse(previous.created_at)) return;
    modalUpdates.current += 1;
    accept({ ...next, volume_title: dialog?.title });
  }, [accept, dialog?.title]);
  const value = useMemo(() => ({ openVolume, revision, job, error, openDetails }), [openVolume, revision, job, error, openDetails]);

  return <Context.Provider value={value}>
    {children}
    {novelId && dialog && <VolumeWriting key={`${novelId}:${dialog.id}`} novelId={novelId} volumeId={dialog.id} volumeTitle={dialog.title}
      open={active} dirty={dirty} onClose={() => setDialog(null)} onJobChange={report} />}
  </Context.Provider>;
}

/** 仅显示未结束的任务；连接异常时将上次状态标为待确认。 */
export function VolumeWritingStatus() {
  const { job, error, openDetails } = useVolumeWriting();
  const t = useTranslations("writing.volumeWriting");
  const locale = useLocale();
  if (!job?.active) return null;
  const running = job?.status === "running" && !error;
  const state = error ? "unknown" : job?.cancel_requested && running ? "ending" : job?.pause_requested && running ? "pausing" : job!.status;
  const warning = Boolean(error || job?.status === "failed");
  const color = warning ? "border-danger/30 bg-danger/5" : "border-border bg-surface-secondary/60";
  return <section aria-label={t("monitor.title")} className={`shrink-0 border-b px-4 py-3 sm:px-6 ${color}`}>
    <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-4 gap-y-2">
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex min-w-0 flex-wrap items-center gap-2 text-sm">
          {running ? <span aria-hidden="true" className="size-3.5 shrink-0 animate-spin rounded-full border-2 border-border border-t-accent motion-reduce:animate-none" />
            : <span aria-hidden="true" className={`size-2 shrink-0 rounded-full ${warning ? "bg-danger" : "bg-muted"}`} />}
          <span className="font-semibold">{t("title")}</span>
          <span role="status" aria-live="polite" className={warning ? "font-medium text-danger" : "font-medium text-accent"}>{t(`monitor.state.${state}`)}</span>
          {job && <span className="min-w-0 truncate text-muted" title={job.volume_title}>{job.volume_title}</span>}
        </div>
        {job && <p className="text-xs tabular-nums text-muted">{job.complete_planning
          ? t("monitor.fullProgress", { completed: job.completed_arcs ?? 0, total: job.summary.arcs ?? 0, written: job.written_chapters, skipped: job.skipped_chapters })
          : t("monitor.progress", { scenes: job.completed_scenes, total: job.summary.scenes, written: job.written_chapters, skipped: job.skipped_chapters })}</p>}
        {running && job?.current && <p className="truncate text-xs text-muted" title={`${job.current.title} · ${job.current.node}`}>
          {t(`stage.${job.current.stage}`)} · {job.current.title}{job.current.node && ` · ${job.current.node}`}
        </p>}
        {job?.status === "failed" && job.error && !error && <p className="break-words text-xs text-danger">{job.error.message}</p>}
        {error && <p className="text-xs text-danger">{t("monitor.connectionLost")}{job && ` ${t("monitor.lastUpdate", { time: new Date(job.updated_at).toLocaleTimeString(locale) })}`}</p>}
      </div>
      {job && <Button size="sm" variant="outline" className="min-h-10 shrink-0" onPress={openDetails}>{t("monitor.details")}</Button>}
    </div>
    {running && job && <progress aria-label={t(job.complete_planning ? "arcProgress" : "sceneProgress")} className="mt-2 block h-1 w-full accent-accent"
      max={Math.max(1, job.complete_planning ? job.summary.arcs ?? 0 : job.summary.scenes)} value={job.complete_planning ? job.completed_arcs ?? 0 : job.completed_scenes} />}
  </section>;
}
