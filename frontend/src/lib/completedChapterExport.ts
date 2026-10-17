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
export async function prepareCompletedChapterExport(novelId: string, title: string, locale: string) {
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
  if (chapters.length === 0) return null;
  const chinese = locale.startsWith("zh");
  const sections = chapters.map((chapter) => {
    const prefix = chinese ? `第${chapter.number}章` : `Chapter ${chapter.number}`;
    const heading = chapter.title.startsWith(prefix) ? chapter.title : `${prefix} ${chapter.title}`;
    return `${heading}\n\n${chapter.content}`;
  });
  // Windows 记事本使用 UTF-8 BOM（由调用方添加），正文缩进和空行保持原样。
  const text = `${title}\n\n${sections.join("\n\n")}\n`.replace(/\r\n|\r/g, "\n").replace(/\n/g, "\r\n");
  const safeTitle = title.replace(/[\\/:*?"<>|\x00-\x1f]/g, "_").replace(/[ .]+$/g, "").slice(0, 100) || "novel";
  const filename = `${safeTitle}-${chinese ? "已完成章节" : "completed-chapters"}.txt`;
  return { text, filename, chapterCount: chapters.length };
}
