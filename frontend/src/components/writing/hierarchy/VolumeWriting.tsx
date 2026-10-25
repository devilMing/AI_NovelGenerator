"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Modal } from "@heroui/react";
import { useTranslations } from "next-intl";
import { RetainedModalBackdrop } from "@/components/layout/RetainedViews";
import { RelationEditorErrorSummary } from "@/components/shared/RelationEditorPrimitives";
import { DEFAULT_GENERATION_PARAMS, GenerationParamsCollapse, buildGenerationParamsPayload } from "@/components/shared/GenerationParamsCollapse";
import { ApiRequestError, apiGet, apiPost } from "@/lib/api";
import VolumeWritingLog from "./VolumeWritingLog";
import type { VolumeWritingJob as Job, VolumeWritingSummary as Summary } from "@/types/volumeWriting";

/** 任务在后端运行；关闭面板不取消，刷新后继续读取同一任务。 */
export default function VolumeWriting({ novelId, volumeId, volumeTitle, open, dirty, onClose, onJobChange }: {
  novelId: string; volumeId: string; volumeTitle: string; open: boolean; dirty: boolean;
  onClose: () => void; onJobChange: (job: Job | null) => void;
}) {
  const t = useTranslations("writing.volumeWriting");
  const base = `/api/novels/${novelId}/volumes/${volumeId}/writing-job`;
  const [summary, setSummary] = useState<Summary | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [readError, setReadError] = useState("");
  const [guidance, setGuidance] = useState("");
  const [params, setParams] = useState(DEFAULT_GENERATION_PARAMS);
  const [paramsOpen, setParamsOpen] = useState(false);
  const [confirmEnd, setConfirmEnd] = useState(false);
  const startKey = useRef<string | null>(null);
  const frozenStart = useRef<Record<string, unknown> | null>(null);
  const operation = useRef(false);
  const change = useRef(onJobChange); change.current = onJobChange;
  const accept = useCallback((next: Job | null) => {
    setJob(next);
    change.current(next);
  }, []);

  useEffect(() => {
    if (!open) return;
    let active = true;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const read = async (initial: boolean) => {
      try {
        const response = await apiGet<{ job: Job | null }>(base, { signal: controller.signal });
        if (!active) return;
        accept(response.job);
        if (initial || !response.job?.active) {
          const preview = await apiGet<Summary>(`${base}/preview`, { signal: controller.signal });
          if (active) setSummary(preview);
        }
        if (active) setReadError("");
      } catch (failure) { if (active) setReadError(failure instanceof Error ? failure.message : t("readFailed")); }
      finally { if (active) { setLoading(false); timer = setTimeout(() => void read(false), 2000); } }
    };
    setLoading(true); void read(true);
    return () => { active = false; controller.abort(); clearTimeout(timer); };
  }, [open, base, accept, t]);

  const act = async (action: "start" | "pause" | "resume" | "cancel") => {
    if (operation.current || (action === "start" || action === "resume") && dirty) return;
    operation.current = true; setBusy(true); setError("");
    try {
      let result: { job: Job };
      if (action === "start") {
        startKey.current ??= crypto.randomUUID();
        const generationParams = { ...buildGenerationParamsPayload(params) };
        delete generationParams.use_stream;
        frozenStart.current ??= { complete_planning: true, user_guidance: guidance, generation_params: generationParams };
        result = await apiPost(base, frozenStart.current, { headers: { "Idempotency-Key": startKey.current } });
        startKey.current = null; frozenStart.current = null;
      } else result = await apiPost(`${base}/${job!.id}/${action}`, {});
      accept(result.job); setConfirmEnd(false);
    } catch (failure) {
      if (action === "start" && failure instanceof ApiRequestError && failure.status < 500) { startKey.current = null; frozenStart.current = null; }
      setError(failure instanceof Error ? failure.message : t("actionFailed"));
    } finally { operation.current = false; setBusy(false); }
  };
  const running = job?.status === "running";
  const canStart = !job?.active;
  const info = job?.active ? job.summary : summary;
  const stopping = job?.pause_requested || job?.cancel_requested;

  return <RetainedModalBackdrop isOpen={open} onOpenChange={(value) => !value && onClose()}>
    <Modal.Container size="lg" scroll="inside" className="p-0 sm:p-4">
      <Modal.Dialog className="h-[100dvh] w-full sm:h-auto sm:max-h-[90vh] sm:max-w-2xl">
        <Modal.Header className="border-b border-border px-5 py-4 sm:px-7">
          <Modal.Heading className="text-lg font-semibold">{t("title")} · {volumeTitle}</Modal.Heading>
          <Modal.CloseTrigger aria-label={t("close")} />
        </Modal.Header>
        <Modal.Body className="workspace-scrollbar space-y-4 px-5 py-5 sm:px-7">
          <p className="text-sm leading-6 text-muted">{t(job?.active && !job.complete_planning ? "legacyDescription" : "description")}</p>
          {dirty && <p role="alert" className="text-sm text-danger">{t("dirty")}</p>}
          {(error || readError) && <RelationEditorErrorSummary message={error || readError} />}
          {job?.error && <RelationEditorErrorSummary message={job.error.message} fieldErrors={job.error.field_errors} />}
          {loading ? <p role="status">{t("loading")}</p> : <>
            {info && <dl className="grid grid-cols-2 gap-3 rounded-xl border border-border bg-surface-secondary p-4 text-sm sm:grid-cols-4">
              {(["scenes", "written", "missing_plans", "pending_bodies"] as const).map((key) => <div key={key}><dt className="text-xs text-muted">{t(`counts.${key}`)}</dt><dd className="mt-1 text-xl font-semibold tabular-nums">{info[key]}</dd></div>)}
            </dl>}
            {info?.complete_planning && <p className="text-sm text-muted">{t("preparationSummary", {
              arcs: info.arcs ?? 0, plots: info.missing_arc_plots ?? 0, scenes: info.arcs_without_scenes ?? 0,
            })}{info.volume_plot_missing && ` ${t("missingVolumePlot")}`}{!info.arcs && ` ${t("missingArcs")}`}</p>}
            {job && <section aria-live="polite" className="space-y-2 rounded-xl border border-border p-4">
              <p className="font-medium">{t(job.status === "completed" && !job.complete_planning ? "legacyCompleted" : `status.${job.status}`)}</p>
              <p className="text-sm tabular-nums">{t("progress", { scenes: job.completed_scenes, total: job.summary.scenes, plans: job.created_chapters, written: job.written_chapters, skipped: job.skipped_chapters })}</p>
              {job.complete_planning && <p className="text-sm tabular-nums">{t("preparationProgress", {
                completed: job.completed_arcs ?? 0, total: job.summary.arcs ?? 0, arcs: job.created_arcs ?? 0, scenes: job.created_scenes ?? 0,
              })}</p>}
              <progress aria-label={t(job.complete_planning ? "arcProgress" : "sceneProgress")} className="h-2 w-full accent-accent"
                max={Math.max(1, job.complete_planning ? job.summary.arcs ?? 0 : job.summary.scenes)} value={job.complete_planning ? job.completed_arcs ?? 0 : job.completed_scenes} />
              {job.current && <p className="break-words text-sm">{t(`stage.${job.current.stage}`)} · {job.current.title}{job.current.node && ` · ${job.current.node}`}</p>}
              {job.has_saved_candidate && job.status !== "completed" && <p className="text-xs text-muted">{t("candidateKept")}</p>}
              {stopping && running && <p role="status" className="text-sm text-muted">{t("stopping")}</p>}
            </section>}
            {job && <VolumeWritingLog key={job.id} base={base} jobId={job.id} running={running} />}
            {canStart && <>
              <label className="block space-y-2 text-sm"><span>{t("guidance")}</span><textarea value={guidance} disabled={busy || frozenStart.current !== null} maxLength={5000} rows={3} onChange={(event) => setGuidance(event.target.value)} className="w-full rounded-xl border border-border bg-background p-3" /></label>
              <p className="text-xs leading-5 text-muted">{t("defaults")}</p>
              <GenerationParamsCollapse value={params} onChange={setParams} isOpen={paramsOpen} onOpenChange={setParamsOpen} disabled={busy || frozenStart.current !== null} />
            </>}
            <p className="text-xs leading-5 text-muted">{t("recovery")}</p>
            {confirmEnd && <p role="alert" className="text-sm">{t("endConfirm")}</p>}
          </>}
        </Modal.Body>
        <Modal.Footer className="flex flex-wrap justify-end gap-2 border-t border-border px-5 py-4 sm:px-7">
          <Button size="sm" variant="ghost" onPress={onClose}>{t(running ? "closeRunning" : "close")}</Button>
          {job?.active && (confirmEnd ? <><Button size="sm" variant="ghost" isDisabled={busy} onPress={() => setConfirmEnd(false)}>{t("keepTask")}</Button><Button size="sm" variant="outline" isDisabled={busy} onPress={() => void act("cancel")}>{t("confirmEnd")}</Button></>
            : <Button size="sm" variant="ghost" isDisabled={busy || Boolean(stopping)} onPress={() => setConfirmEnd(true)}>{t("end")}</Button>)}
          {running && <Button size="sm" variant="outline" isDisabled={busy || Boolean(stopping)} onPress={() => void act("pause")}>{t("pause")}</Button>}
          {job?.active && !running && <Button size="sm" variant="primary" isDisabled={busy || dirty || Boolean(readError)} onPress={() => void act("resume")}>{t("resume")}</Button>}
          {canStart && <Button size="sm" variant="primary" isDisabled={busy || loading || dirty || !summary || Boolean(readError)} onPress={() => void act("start")}>{t("start")}</Button>}
        </Modal.Footer>
      </Modal.Dialog>
    </Modal.Container>
  </RetainedModalBackdrop>;
}
