import { apiGet } from "@/lib/api";

interface ChapterSummary {
  chapter_id: string;
  novel_id: string;
  chapter_number: number;
  version: number;
  is_deleted: boolean;
}

interface SavedChapter extends ChapterSummary {
  title: string;
  content: string;
}

export class ChapterExportChangedError extends Error {}

/** 按服务端阅读顺序读取已保存正文；任一章读取失败时不生成残缺文件。 */
async function readCompletedChapters(novelId: string) {
  const directory = await apiGet<{ data: ChapterSummary[] }>(`/api/chapters/novel/${novelId}`);
  const chapters: { number: number; title: string; content: string }[] = [];
  const rows = directory.data.filter((row) => !row.is_deleted);
  // 限制并发，避免长篇作品一次请求全部章节；Promise.all 保持目录顺序。
  for (let offset = 0; offset < rows.length; offset += 4) {
    const batch = await Promise.all(rows.slice(offset, offset + 4).map(async (row) => {
      const saved = await apiGet<SavedChapter>(`/api/chapters/${row.chapter_id}`);
      if (saved.chapter_id !== row.chapter_id || saved.novel_id !== novelId || saved.is_deleted || saved.version !== row.version) {
        throw new ChapterExportChangedError();
      }
      return { number: row.chapter_number, title: saved.title, content: saved.content };
    }));
    chapters.push(...batch.filter((chapter) => chapter.content.trim().length > 0));
  }
  // 再核对目录，防止读取后续章节期间前面的正文、章节顺序或回收站状态发生变化。
  const latest = await apiGet<{ data: ChapterSummary[] }>(`/api/chapters/novel/${novelId}`);
  const current = latest.data.filter((row) => !row.is_deleted);
  if (current.length !== rows.length || current.some((row, index) => row.chapter_id !== rows[index].chapter_id
    || row.version !== rows[index].version || row.chapter_number !== rows[index].chapter_number)) {
    throw new ChapterExportChangedError();
  }
  return chapters;
}

function chapterSection(chapter: { number: number; title: string; content: string }, chinese: boolean) {
  const prefix = chinese ? `第${chapter.number}章` : `Chapter ${chapter.number}`;
  const heading = chapter.title.startsWith(prefix) ? chapter.title : `${prefix} ${chapter.title}`;
  return `${heading}\n\n${chapter.content}`;
}

function windowsLines(text: string) {
  return text.replace(/\r\n|\r/g, "\n").replace(/\n/g, "\r\n");
}

/** 保持原合并 TXT 导出的格式。 */
export async function prepareCompletedChapterExport(novelId: string, title: string, locale: string) {
  const chapters = await readCompletedChapters(novelId);
  if (chapters.length === 0) return null;
  const chinese = locale.startsWith("zh");
  const sections = chapters.map((chapter) => chapterSection(chapter, chinese));
  // Windows 记事本使用 UTF-8 BOM（由调用方添加），正文缩进和空行保持原样。
  const text = windowsLines(`${title}\n\n${sections.join("\n\n")}\n`);
  const safeTitle = title.replace(/[\\/:*?"<>|\x00-\x1f]/g, "_").replace(/[ .]+$/g, "").slice(0, 100) || "novel";
  const filename = `${safeTitle}-${chinese ? "已完成章节" : "completed-chapters"}.txt`;
  return { text, filename, chapterCount: chapters.length };
}

function safeFilenameStem(title: string, fallback: string) {
  let stem = title.normalize("NFC").replace(/[\\/:*?"<>|\x00-\x1f\x7f]/g, "_").trim().replace(/[ .]+$/g, "");
  if (!stem) stem = fallback;
  if (/^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)/i.test(stem)) stem = `_${stem}`;
  // 留出扩展名与重名后缀的空间；按 UTF-8 字节截断，保持中文和 emoji 完整。
  const encoder = new TextEncoder();
  let length = 0;
  let shortened = "";
  for (const character of stem) {
    length += encoder.encode(character).length;
    if (length > 180) break;
    shortened += character;
  }
  return shortened.replace(/[ .]+$/g, "") || fallback;
}

/** 文件名使用实际章节号，直接接章节标题。 */
export async function prepareCompletedChaptersZip(novelId: string, title: string, locale: string) {
  const chapters = await readCompletedChapters(novelId);
  if (chapters.length === 0) return null;
  const { zip, strToU8 } = await import("fflate");
  const files: Record<string, Uint8Array> = Object.create(null);
  const used = new Set<string>();
  const chinese = locale.startsWith("zh");
  for (const chapter of chapters) {
    const fallback = chinese ? `第${chapter.number}章` : `Chapter ${chapter.number}`;
    const stem = `${chapter.number}${safeFilenameStem(chapter.title, fallback)}`;
    let name = `${stem}.txt`;
    let suffix = 2;
    while (used.has(name.normalize("NFC").toLowerCase())) name = `${stem} (${suffix++}).txt`;
    used.add(name.normalize("NFC").toLowerCase());
    files[name] = strToU8(`\uFEFF${windowsLines(`${chapterSection(chapter, chinese)}\n`)}`);
  }
  const bytes = await new Promise<Uint8Array<ArrayBuffer>>((resolve, reject) => {
    zip(files, { level: 6 }, (error, data) => error ? reject(error) : resolve(Uint8Array.from(data)));
  });
  const filename = `${safeFilenameStem(title, "novel")}-${chinese ? "分章TXT" : "chapter-txt"}.zip`;
  return { blob: new Blob([bytes], { type: "application/zip" }), filename, chapterCount: chapters.length };
}
