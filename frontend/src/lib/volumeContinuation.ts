type Reader = (path: string) => Promise<unknown>;
const objectId = /^[a-f0-9]{24}$/i;

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("写作目标无效 / Invalid writing target");
  return value as Record<string, unknown>;
}

/** Resolve only; opening the existing volume panel must never start or replay generation. */
export async function resolveVolumeContinuation(novelId: string, chapterId: string | undefined, read: Reader): Promise<{ id: string; title: string }> {
  const overview = record(await read(`/api/novels/${novelId}/volume-writing-job`));
  const current = overview.job ? record(overview.job) : null;
  let volumeId: unknown;
  if (current?.active === true) {
    if (current.novel_id !== novelId) throw new Error("任务不属于当前小说 / Run belongs to another novel");
    volumeId = current.volume_id;
  } else {
    if (!chapterId || !objectId.test(chapterId)) throw new Error("请先保存第一章 / Save the first chapter first");
    const chapter = record(await read(`/api/chapters/${chapterId}`));
    if (chapter.novel_id !== novelId) throw new Error("章节不属于当前小说 / Chapter belongs to another novel");
    volumeId = chapter.volume_id;
  }
  if (typeof volumeId !== "string" || !objectId.test(volumeId)) throw new Error("所属卷不存在 / Volume is unavailable");
  const volume = record(await read(`/api/volumes/${volumeId}`));
  if (volume._id !== volumeId || volume.novel_id !== novelId || typeof volume.title !== "string") throw new Error("所属卷无效 / Invalid parent volume");
  return { id: volumeId, title: volume.title };
}
