// Run after frontend dependency installation: node scripts/test_completed_chapter_export.cjs
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("../frontend/node_modules/typescript");
const fflate = require("../frontend/node_modules/fflate");

const source = fs.readFileSync(path.join(__dirname, "../frontend/src/lib/completedChapterExport.ts"), "utf8");
function load(apiGet) {
  const result = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } });
  const exports = {};
  vm.runInNewContext(result.outputText, { exports, Blob, TextEncoder, Uint8Array,
    require: (name) => name === "fflate" ? fflate : { apiGet } });
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

test("ZIP filenames use chapter numbers directly followed by Chinese titles, preserving gaps", async () => {
  const directory = [row("one", 1), row("empty", 2), row("two", 3), { ...row("deleted", 4), is_deleted: true }];
  const bodies = { one: { ...saved("one", 1, "  中文正文\n\n第二段  "), title: "一纸债期" },
    empty: saved("empty", 2, "  \n"), two: { ...saved("two", 3, "结尾正文\r\n第二行"), title: "雨封账里的活人" } };
  const requests = [];
  const { prepareCompletedChaptersZip } = load(async (url) => {
    requests.push(url);
    return url.includes("/novel/") ? { data: directory } : bodies[url.split("/").pop()];
  });
  const result = await prepareCompletedChaptersZip("book", "我的小说", "zh");
  assert.equal(result.filename, "我的小说-分章TXT.zip");
  assert.equal(result.chapterCount, 2);
  assert.equal(result.blob.type, "application/zip");
  const entries = fflate.unzipSync(new Uint8Array(await result.blob.arrayBuffer()));
  assert.deepEqual(Object.keys(entries), ["1一纸债期.txt", "3雨封账里的活人.txt"]);
  assert.deepEqual([...entries["1一纸债期.txt"].slice(0, 3)], [0xef, 0xbb, 0xbf]);
  // strFromU8 uses TextDecoder, which consumes the BOM already verified above.
  assert.equal(fflate.strFromU8(entries["1一纸债期.txt"]), "第1章 一纸债期\r\n\r\n  中文正文\r\n\r\n第二段  \r\n");
  assert.ok(!requests.some((url) => url.endsWith("deleted")));
});

test("ZIP names avoid case-insensitive and normalized collisions, Windows reserved names and traversal", async () => {
  const titles = ["同名", "同名", "同名 (2)", "Title", "title", "CON", "nul.any", "../路径\\标题:*?\"<>|", "é", "e\u0301", " . ", "😀".repeat(200)];
  const { prepareCompletedChaptersZip } = load(async (url) => {
    if (url.includes("/novel/")) return { data: titles.map((_, index) => row(String(index), index + 1)) };
    const id = Number(url.split("/").pop());
    return { ...saved(String(id), id + 1, `Body-${id}`), title: titles[id] };
  });
  const result = await prepareCompletedChaptersZip("book", "CON", "zh");
  const entries = fflate.unzipSync(new Uint8Array(await result.blob.arrayBuffer()));
  const names = Object.keys(entries);
  assert.equal(names.length, titles.length);
  assert.equal(new Set(names.map((name) => name.normalize("NFC").toLowerCase())).size, titles.length);
  assert.ok(names.includes("1同名.txt") && names.includes("2同名.txt") && names.includes("3同名 (2).txt"));
  assert.ok(names.includes("6_CON.txt") && names.includes("7_nul.any.txt") && names.includes("11第11章.txt"));
  for (const name of names) {
    assert.ok(!/[\\/:*?"<>|\x00-\x1f]/.test(name));
    assert.ok(Buffer.byteLength(name, "utf8") <= 200);
    assert.ok(!/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/.test(name));
  }
  assert.equal(result.filename, "_CON-分章TXT.zip");
  assert.ok(fflate.strFromU8(entries["2同名.txt"]).includes("Body-1"));
});

test("both formats reject changed directory snapshots and fail whole exports on a failed read", async () => {
  for (const format of ["prepareCompletedChapterExport", "prepareCompletedChaptersZip"]) {
    for (const change of ["version", "addition", "deletion", "order"]) {
      let reads = 0;
      const exports = load(async (url) => {
        if (!url.includes("/novel/")) return saved("one", 1, "Body");
        reads++;
        return { data: reads === 1 ? [row("one", 1)] : change === "version" ? [row("one", 1, 2)]
          : change === "addition" ? [row("one", 1), row("two", 2)] : change === "deletion" ? [] : [row("one", 2)] };
      });
      await assert.rejects(exports[format]("book", "Book", "en"), exports.ChapterExportChangedError);
    }
    const exports = load(async (url) => {
      if (url.includes("/novel/")) return { data: [row("one", 1)] };
      throw new Error("failed chapter read");
    });
    await assert.rejects(exports[format]("book", "Book", "en"), /failed chapter read/);
  }
});

test("empty books do not create an empty ZIP and chapter text is actually compressed", async () => {
  const empty = load(async () => ({ data: [] }));
  assert.equal(await empty.prepareCompletedChaptersZip("book", "Book", "en"), null);
  const text = "Repeated Chinese text 中文正文\n".repeat(1000);
  const exports = load(async (url) => url.includes("/novel/") ? { data: [row("one", 1)] } : saved("one", 1, text));
  const result = await exports.prepareCompletedChaptersZip("book", "Book", "en");
  assert.ok(result.blob.size < Buffer.byteLength(text, "utf8") / 2);
  const entries = fflate.unzipSync(new Uint8Array(await result.blob.arrayBuffer()));
  assert.ok(fflate.strFromU8(entries["1Title one.txt"]).includes("中文正文"));
});

test("chapter numbers are exported exactly without padding or added separators", async () => {
  const numbers = [1, 2, 9, 10, 11, 99, 100, 999, 1000];
  const exports = load(async (url) => {
    if (url.includes("/novel/")) return { data: numbers.map((number) => row(String(number), number)) };
    const number = Number(url.split("/").pop());
    return { ...saved(String(number), number, `Body ${number}`), title: "相同标题" };
  });
  const result = await exports.prepareCompletedChaptersZip("book", "Book", "zh");
  const names = Object.keys(fflate.unzipSync(new Uint8Array(await result.blob.arrayBuffer())));
  assert.deepEqual(names, numbers.map((number) => `${number}相同标题.txt`));
  assert.equal(names[0], "1相同标题.txt");
  assert.equal(names[4], "11相同标题.txt");
});
