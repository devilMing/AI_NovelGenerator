// Run after frontend dependency installation: node scripts/test_completed_chapter_export.cjs
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("../frontend/node_modules/typescript");

const source = fs.readFileSync(path.join(__dirname, "../frontend/src/lib/completedChapterExport.ts"), "utf8");
function load(apiGet) {
  const result = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } });
  const exports = {};
  vm.runInNewContext(result.outputText, { exports, require: () => ({ apiGet }) });
  return exports;
}
const row = (id, number, version = 1) => ({ chapter_id: id, novel_id: "book", chapter_number: number, version, is_deleted: false });
const saved = (id, number, content, version = 1) => ({ ...row(id, number, version), title: `Title ${id}`, content });

test("exports reading order and exact saved paragraphs, excluding empty and recycled chapters", async () => {
  const requests = [];
  const directory = [row("first", 2), row("empty", 4), row("last", 7), { ...row("trashed", 8), is_deleted: true }];
  const bodies = { first: saved("first", 2, "  中文正文\n\n第二段  "), empty: saved("empty", 4, " \n\t"), last: saved("last", 7, "Last paragraph") };
  const { prepareCompletedChapterExport } = load(async (url) => {
    requests.push(url);
    return url.includes("/novel/") ? { data: directory } : bodies[url.split("/").pop()];
  });
  const result = await prepareCompletedChapterExport("book", 'Book:/?*"', "zh-CN");
  assert.equal(result.chapterCount, 2);
  assert.match(result.text, /第2章 Title first\r\n\r\n  中文正文\r\n\r\n第二段  /);
  assert.ok(result.text.indexOf("第2章") < result.text.indexOf("第7章"));
  assert.ok(!result.text.includes("Title empty"));
  assert.ok(!requests.some((request) => request.endsWith("trashed")));
  assert.ok(result.filename.endsWith("-已完成章节.txt"));
  assert.ok(!/[\\/:*?"<>|]/.test(result.filename));
});

test("empty books produce no download payload", async () => {
  const { prepareCompletedChapterExport } = load(async () => ({ data: [] }));
  assert.equal(await prepareCompletedChapterExport("book", "Book", "en"), null);
});

test("a failed chapter aborts the entire export instead of silently dropping it", async () => {
  const { prepareCompletedChapterExport } = load(async (url) => {
    if (url.includes("/novel/")) return { data: [row("ok", 1), row("fail", 2)] };
    if (url.endsWith("fail")) throw new Error("network failure");
    return saved("ok", 1, "Saved text");
  });
  await assert.rejects(prepareCompletedChapterExport("book", "Book", "en"), /network failure/);
});

test("changed or out-of-scope chapter responses abort export", async () => {
  for (const overrides of [{ version: 2 }, { novel_id: "other-book" }, { is_deleted: true }, { chapter_id: "other" }]) {
    const { prepareCompletedChapterExport, ChapterExportChangedError } = load(async (url) =>
      url.includes("/novel/") ? { data: [row("one", 1)] } : { ...saved("one", 1, "Text"), ...overrides });
    await assert.rejects(prepareCompletedChapterExport("book", "Book", "en"), ChapterExportChangedError);
  }
});

test("large books read at most four chapters at a time and preserve order", async () => {
  let active = 0;
  let maximum = 0;
  const directory = Array.from({ length: 11 }, (_, index) => row(String(index), index + 1));
  const { prepareCompletedChapterExport } = load(async (url) => {
    if (url.includes("/novel/")) return { data: directory };
    active += 1;
    maximum = Math.max(maximum, active);
    const id = url.split("/").pop();
    await new Promise((resolve) => setTimeout(resolve, (4 - Number(id) % 4) * 2));
    active -= 1;
    return saved(id, Number(id) + 1, `Text ${id}`);
  });
  const result = await prepareCompletedChapterExport("book", "Book", "en");
  assert.equal(result.chapterCount, 11);
  assert.equal(maximum, 4);
  assert.ok(result.text.indexOf("Chapter 2 ") < result.text.indexOf("Chapter 10 "));
});
