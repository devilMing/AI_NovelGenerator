"use client";

import { useEffect, useRef, useState } from "react";
import { useLocale } from "next-intl";
import { Button, Modal } from "@heroui/react";
import { useRouter } from "next/navigation";
import { RetainedModalBackdrop } from "@/components/layout/RetainedViews";
import { apiGet, apiPost, ApiRequestError } from "@/lib/api";
import FirstChapterTimeline from "./FirstChapterTimeline";
import { canStartFirstChapter } from "@/lib/firstChapterTimeline";
import { notifyResourceChange } from "@/lib/resourceChanges";
import { resolveVolumeContinuation } from "@/lib/volumeContinuation";
import { useVolumeWriting } from "@/components/writing/hierarchy/VolumeWritingStatus";

interface Job {
  id: string; status: string; active: boolean; pause_requested: boolean; cancel_requested: boolean;
  current: { label: string; stage: string } | null; completed_steps: string[];
  error: { message: string } | null; has_saved_candidate: boolean; chapter_id?: string; created_at: string;
}

/** Backend job survives closing this panel; requests keep a stable start key. */
export default function FirstChapterWriting({ novelId, disabled, onSaved }: { novelId: string; disabled: boolean; onSaved?: () => void }) {
  const locale = useLocale();
  const en = locale === "en";
  const text = (zh: string, english: string) => en ? english : zh;
  const router = useRouter();
  const { openVolume } = useVolumeWriting();
  const [open, setOpen] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [guidance, setGuidance] = useState("");
  const [optional, setOptional] = useState(true);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [history, setHistory] = useState<Job[]>([]);
  const [logJobId, setLogJobId] = useState("");
  const stable = useRef<{ key: string; body: object } | null>(null);
  const sending = useRef(false);
  const continuation = useRef<AbortController | null>(null);
  useEffect(() => () => continuation.current?.abort(), [open, novelId]);
  const savedCount = useRef(0);
  const onChange = useRef(onSaved); onChange.current = onSaved;
  const base = `/api/novels/${novelId}/first-chapter-job`;
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    async function read() {
      try {
        const result = await apiGet<{ job: Job | null }>(base, { signal: controller.signal });
        if (!active) return;
        setJob(result.job); setError("");
        const count = result.job?.completed_steps.length ?? 0;
        if (count !== savedCount.current) { savedCount.current = count; notifyResourceChange({ novelId, source: "manual" }); onChange.current?.(); }
        if (open) {
          const old = await apiGet<{ jobs: Job[] }>(`${base}/history`, { signal: controller.signal });
          if (active) setHistory(old.jobs);
        }
      } catch (failure) { if (active) setError(failure instanceof Error ? failure.message : String(failure)); }
      finally { if (active) { setLoading(false); timer = setTimeout(() => void read(), open ? 2000 : 10000); } }
    }
    setLoading(true); void read();
    return () => { active = false; controller.abort(); clearTimeout(timer); };
  }, [base, open, novelId]);
  async function act(action: "start" | "pause" | "resume" | "cancel") {
    if (sending.current) return;
    sending.current = true; setBusy(true); setError("");
    try {
      if (action === "start") stable.current ??= { key: crypto.randomUUID(), body: { user_guidance: guidance, include_optional: optional } };
      const result = action === "start"
        ? await apiPost<{ job: Job }>(base, stable.current!.body, { headers: { "Idempotency-Key": stable.current!.key } })
        : await apiPost<{ job: Job }>(`${base}/${job!.id}/${action}`, {});
      setJob(result.job); stable.current = null; setConfirmCancel(false);
    } catch (failure) {
      if (action === "start" && failure instanceof ApiRequestError && failure.status < 500) stable.current = null;
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally { sending.current = false; setBusy(false); }
  }
  async function continueWriting() {
    if (sending.current || disabled || job?.status !== "completed") return;
    sending.current = true; setBusy(true); setError("");
    const controller = new AbortController();
    continuation.current = controller;
    try {
      const target = await resolveVolumeContinuation(novelId, job.chapter_id, (path) => apiGet(path, { signal: controller.signal }));
      if (controller.signal.aborted) return;
      setOpen(false);
      openVolume(target);
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : String(failure));
    } finally { sending.current = false; setBusy(false); }
  }
  const statuses: Record<string, string> = { running: text("进行中", "Running"), paused: text("已暂停", "Paused"), failed: text("失败，进度已保留", "Failed — progress retained"), completed: text("第一章已保存", "First chapter saved"), cancelled: text("已结束", "Ended") };
  const stages: Record<string, string> = { generating: text("生成", "Generating"), reviewing: text("一致性检查", "Consistency review"), repairing: text("根据证据修订", "Repairing with evidence"), saving: text("保存", "Saving") };
  const canStart = canStartFirstChapter(job);
  return <>
    <Button size="sm" variant={job?.active ? "primary" : "outline"} isDisabled={disabled} onPress={() => setOpen(true)}>
      {job?.active ? text("一键写作 · 查看进度", "One-click writing · Progress") : text("一键写作", "One-click writing")}
    </Button>
    <RetainedModalBackdrop isOpen={open} onOpenChange={setOpen}>
      <Modal.Container size="lg" scroll="inside" className="p-0 sm:p-4">
        <Modal.Dialog className="h-[100dvh] w-full sm:h-auto sm:max-h-[90vh] sm:max-w-3xl">
          <Modal.Header className="border-b border-border px-5 py-4 sm:px-7"><Modal.Heading className="text-lg font-semibold">{text("一键写作到第一章", "Write through the first chapter")}</Modal.Heading><Modal.CloseTrigger aria-label={text("关闭", "Close")} /></Modal.Header>
          <Modal.Body className="workspace-scrollbar min-h-0 space-y-4 px-5 py-5 sm:px-7">
            <p className="text-sm leading-6 text-muted">{text("从已保存的小说信息开始，按顺序补齐资料、卷与篇章剧情、场景及章计划，检查一致性后保存第一章。已有资料和正文不会覆盖。", "Prepare missing resources and plans from the saved novel, review consistency, then save the first chapter. Existing content is preserved.")}</p>
            <p className="text-xs leading-5 text-muted">{text("使用设置中的默认 AI 流程及其模型。每步在后端保存；关闭面板不停止。暂停在当前模型步骤完成并保留候选后生效；服务重启后可继续。", "Uses configured default flows and models. Work is saved on the backend. Closing this panel does not stop it. Pause takes effect at a safe checkpoint; resume after a restart.")}</p>
            {disabled && <p role="alert">{text("请先保存当前编辑。", "Save your current edits first.")}</p>}
            {(error || job?.error) && <p role="alert" className="whitespace-pre-wrap text-sm text-danger">{error || job?.error?.message}</p>}
            {loading && <p role="status">{text("读取任务…", "Loading…")}</p>}
            {job && <section aria-live="polite" className="space-y-2 rounded-xl border border-border p-4">
              <p className="text-xs text-muted">{text("当前写作任务", "Current writing run")}</p>
              <p className="font-medium">{statuses[job.status] ?? job.status}</p>
              <p className="text-sm">{text(`已保存 ${job.completed_steps.length} 步`, `${job.completed_steps.length} steps saved`)}</p>
              {job.status === "completed" && <p className="text-xs leading-5 text-muted">{text("继续一键写作会进入逐卷面板，先核对待写范围；已有正文自动跳过，仅补齐本卷已保存场景中的缺项。", "Continue opens the volume panel to confirm its scope. Existing text is skipped; only missing work in saved scenes is filled.")}</p>}
              {job.current && <p className="text-sm">{job.current.label} · {stages[job.current.stage] ?? job.current.stage}</p>}
              {job.has_saved_candidate && <p className="text-xs text-muted">{text("当前候选已保留，继续时优先核对并保存。", "Candidate retained; resume reviews and saves it first.")}</p>}
              {(job.pause_requested || job.cancel_requested) && job.status === "running" && <p>{text("正在等待安全检查点…", "Waiting for a safe checkpoint…")}</p>}
              <details><summary className="cursor-pointer text-sm">{text("已保存步骤", "Saved steps")}</summary><ol className="list-inside list-decimal text-sm">{job.completed_steps.map((step, index) => <li key={index}>{step}</li>)}</ol></details>
            </section>}
            {job && <>
              {history.length > 1 && <label className="block space-y-2 text-sm"><span>{text("查看日志的任务（按开始时间）", "Run logs (start time)")}</span><select className="w-full rounded-lg border border-border bg-background p-2" value={logJobId || job.id} onChange={(e) => setLogJobId(e.target.value)}>{history.map((run) => <option key={run.id} value={run.id}>{text("开始于 ", "Started ")}{new Date(run.created_at).toLocaleString(locale)} · {statuses[run.status] ?? run.status}</option>)}</select></label>}
              <FirstChapterTimeline key={logJobId || job.id} base={base} jobId={logJobId || job.id} running={(!logJobId || logJobId === job.id) && job.status === "running"} />
            </>}
            {canStart && <>
              <label className="block space-y-2 text-sm"><span>{text("写作要求（可选）", "Writing guidance (optional)")}</span><textarea rows={3} maxLength={1000} className="w-full rounded-lg border border-border bg-background p-3" value={guidance} disabled={busy || stable.current !== null} onChange={(e) => setGuidance(e.target.value)} /></label>
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={optional} disabled={busy || stable.current !== null} onChange={(e) => setOptional(e.target.checked)} />{text("尚无卷规划时补齐人物关系、世界规则和物品", "Prepare missing relationships, rules and items before volume planning")}</label>
            </>}
            <p className="text-xs leading-5 text-muted">{text("检查依据限于本步真实读取的资料；发现冲突最多修订两次，仍不通过会暂停并保留引证。资料缺项或人工修改冲突不会擅自补成事实。首章审校记录会绑定正文历史版本。", "Reviews use actual step sources. Up to two evidence-based repairs; unresolved conflicts stop with evidence retained. Missing facts and edits are not silently overridden. The chapter review is bound to its saved revision.")}</p>
            {confirmCancel && <p role="alert">{text("结束本轮会保留所有已保存内容和日志。", "Ending retains all saved content and logs.")}</p>}
          </Modal.Body>
          <Modal.Footer className="flex flex-wrap justify-end gap-2 border-t border-border bg-surface-raised px-5 py-4 sm:px-7">
            <Button size="sm" variant="ghost" onPress={() => setOpen(false)}>{text("关闭", "Close")}</Button>
            {job?.status === "completed" && <Button size="sm" variant="outline" onPress={() => { setOpen(false); router.push(`/${en ? "en" : "zh"}/writing/${novelId}?workspace=chapter-editor&chapter_id=${job.chapter_id}`); }}>{text("查看第一章", "View first chapter")}</Button>}
            {job?.status === "completed" && <Button size="sm" variant="primary" isDisabled={busy || disabled || loading} onPress={() => void continueWriting()}>{text("继续一键写作", "Continue one-click writing")}</Button>}
            {job?.active && <Button size="sm" variant="ghost" isDisabled={busy} onPress={() => confirmCancel ? void act("cancel") : setConfirmCancel(true)}>{confirmCancel ? text("确认结束", "Confirm end") : text("结束本轮", "End run")}</Button>}
            {job?.status === "running" && <Button size="sm" variant="outline" isDisabled={busy || job.pause_requested || job.cancel_requested} onPress={() => void act("pause")}>{text("暂停", "Pause")}</Button>}
            {job?.active && job.status !== "running" && <Button size="sm" variant="primary" isDisabled={busy || disabled || Boolean(error)} onPress={() => void act("resume")}>{text("继续", "Resume")}</Button>}
            {canStart && <Button size="sm" variant="primary" isDisabled={busy || loading || disabled || Boolean(error)} onPress={() => void act("start")}>{text("开始一键写作", "Start writing")}</Button>}
          </Modal.Footer>
        </Modal.Dialog>
      </Modal.Container>
    </RetainedModalBackdrop>
  </>;
}
