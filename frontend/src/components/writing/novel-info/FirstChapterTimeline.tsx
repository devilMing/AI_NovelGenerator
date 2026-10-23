"use client";

import { useEffect, useState } from "react";
import { useLocale } from "next-intl";
import { apiGet } from "@/lib/api";
import { wallDuration } from "@/lib/firstChapterTimeline";
import VolumeWritingLog from "@/components/writing/hierarchy/VolumeWritingLog";

interface Step {
  id: string; title: string; state: string; started_at: string | null; saved_at: string | null;
  elapsed_ms: number | null; node_count: number; failure_count: number; repairs: number;
  review_summary: string; candidate_retained: boolean;
}
interface Timeline { steps: Step[]; history: Step[]; status: string; started_at: string; finished_at: string | null }

function StepItem({ step, index, base, jobId }: { step: Step; index?: number; base: string; jobId: string }) {
  const locale = useLocale();
  const en = locale === "en";
  const [open, setOpen] = useState(false);
  const names: Record<string, string> = {
    saved: en ? "Saved" : "已保存", running: en ? "Running" : "进行中",
    paused: en ? "Paused" : "已暂停", failed: en ? "Failed · progress retained" : "失败 · 进度已保留",
    cancelled: en ? "Ended · not adopted" : "已结束 · 未采用", stopped: en ? "Stopped · not adopted" : "已停止 · 未采用",
    superseded: en ? "Superseded · not adopted" : "已替换 · 未采用",
  };
  const elapsed = wallDuration(step.elapsed_ms);
  const format = (value: string) => new Date(value).toLocaleString(locale);
  return <li className="min-w-0 border-b border-border last:border-b-0">
    <details onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="flex min-h-14 cursor-pointer list-none items-start gap-3 py-4 focus-visible:outline-2 focus-visible:outline-accent">
        <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-border text-xs tabular-nums text-muted" aria-hidden="true">{index == null ? "↺" : index + 1}</span>
        <span className="min-w-0 flex-1 space-y-1">
          <span className="block text-sm font-medium">{step.title}</span>
          <span className="block text-xs leading-5 text-muted">{step.state === "saved" ? (en ? "Result saved automatically" : "结果已自动保存") : step.candidate_retained ? (en ? "Candidate retained; not yet adopted" : "候选已保留，尚未正式采用") : (en ? "No saved result for this step" : "本步尚无正式保存结果")}{elapsed !== null && ` · ${en ? "Elapsed" : "历时"} ${elapsed}`}</span>
          {(step.failure_count > 0 || step.repairs > 0) && <span className="block text-xs text-muted">{en ? `${step.failure_count} historical failure records · ${step.repairs} consistency repairs` : `历史失败 ${step.failure_count} 条 · 一致性修订 ${step.repairs} 次`}</span>}
        </span>
        <span className={`shrink-0 rounded-md border border-border px-2 py-1 text-xs ${step.state === "failed" ? "text-danger" : step.state === "running" ? "text-accent" : "text-muted"}`}>{names[step.state] ?? step.state}</span>
        <span className="shrink-0 pt-1 text-xs text-muted" aria-hidden="true">{open ? "−" : "+"}</span>
      </summary>
      {open && <div className="min-w-0 space-y-3 pb-4 pl-9">
        <dl className="space-y-1 text-xs leading-5 text-muted">
          {step.started_at && <div><dt className="inline">{en ? "Step started: " : "步骤开始："}</dt><dd className="inline">{format(step.started_at)}</dd></div>}
          {step.saved_at && <div><dt className="inline">{en ? "Saved: " : "保存时间："}</dt><dd className="inline">{format(step.saved_at)}</dd></div>}
          {elapsed !== null && <div><dt className="inline">{en ? "Wall time (includes pauses/retries): " : "历时（含暂停与重试）："}</dt><dd className="inline tabular-nums">{elapsed}</dd></div>}
        </dl>
        {step.review_summary && <p className="whitespace-pre-wrap break-words rounded-lg bg-surface-secondary p-3 text-xs leading-6">{step.review_summary}</p>}
        <p className="text-xs leading-5 text-muted">{en ? `${step.node_count} node records. Historical failures remain visible even after this step succeeds.` : `共 ${step.node_count} 条节点记录。历史失败和未结束调用会保留在详情中，不代表本步最终失败。`}</p>
        <VolumeWritingLog base={base} jobId={jobId} stepId={step.id} running={step.state === "running"} defaultExpanded />
      </div>}
    </details>
  </li>;
}

/** Only summary metadata is loaded up front; candidates/node output stay behind step details. */
export default function FirstChapterTimeline({ base, jobId, running }: { base: string; jobId: string; running: boolean }) {
  const locale = useLocale();
  const en = locale === "en";
  const [page, setPage] = useState<Timeline | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    async function read() {
      let keepPolling = running;
      try {
        const result = await apiGet<Timeline>(`${base}/${jobId}/timeline`, { signal: controller.signal });
        if (!active) return;
        setPage(result); setError("");
        keepPolling = result.status === "running";
      } catch (failure) {
        if (active) setError(failure instanceof Error ? failure.message : String(failure));
      } finally {
        if (active && keepPolling) timer = setTimeout(() => void read(), 2000);
      }
    }
    void read();
    return () => { active = false; controller.abort(); clearTimeout(timer); };
  }, [base, jobId, running]);
  return <section className="min-w-0 border-t border-border pt-4" aria-label={en ? "Writing timeline" : "写作进度时间线"}>
    <h3 className="text-sm font-semibold">{en ? "Writing timeline" : "写作进度时间线"}</h3>
    <p className="mt-1 text-xs leading-5 text-muted">{en ? "Steps are shown in execution order. Expand a step for its result, node output and retry history." : "按执行顺序展示；展开步骤可查看结果、节点输出和重试记录。"}</p>
    {error && <p role="alert" className="py-3 text-sm text-danger">{error}</p>}
    {!page && !error && <p role="status" className="py-4 text-sm text-muted">{en ? "Loading timeline…" : "读取时间线…"}</p>}
    {page && <>
      <p className="mt-3 text-xs leading-5 text-muted">{en ? `Selected run: ${page.steps.filter((step) => step.state === "saved").length} steps saved. History records adoption at that time; later edits or rollbacks do not rewrite it.` : `所选任务：当时已保存 ${page.steps.filter((step) => step.state === "saved").length} 步。历史记录不会随之后的人工修改或回退而改写。`}</p>
      <dl className="mt-3 space-y-1 rounded-lg bg-surface-secondary p-3 text-xs leading-5 text-muted">
        <div><dt className="inline">{en ? "Run started: " : "任务开始："}</dt><dd className="inline">{new Date(page.started_at).toLocaleString(locale)}</dd></div>
        {page.finished_at && <div><dt className="inline">{en ? "Run ended: " : "任务结束："}</dt><dd className="inline">{new Date(page.finished_at).toLocaleString(locale)}</dd></div>}
      </dl>
      {!page.steps.length && <p className="py-4 text-sm text-muted">{en ? "No business steps were recorded for this run." : "本轮没有记录业务步骤。"}</p>}
      <ol className="mt-2 min-w-0" aria-label={en ? "Writing steps" : "写作步骤"}>{page.steps.map((step, index) => <StepItem key={step.id} step={step} index={index} base={base} jobId={jobId} />)}</ol>
      {page.history.length > 0 && <details className="mt-4 border-t border-border pt-3">
        <summary className="min-h-10 cursor-pointer text-sm text-muted">{en ? `Superseded work (${page.history.length})` : `历史调整（${page.history.length} 条未采用步骤）`}</summary>
        <ul>{page.history.map((step) => <StepItem key={step.id} step={step} base={base} jobId={jobId} />)}</ul>
      </details>}
    </>}
  </section>;
}
