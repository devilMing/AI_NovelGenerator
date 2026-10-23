import assert from "node:assert/strict";
import test from "node:test";
import { resolveVolumeContinuation } from "./volumeContinuation.ts";

const novel = "6adac1d02bfd16912e2807b6";
const chapter = "6adaef7e9a59daebdde3f371";
const volume = "6adadce4fbbdab9cda77732a";

test("completed first chapter opens its existing volume without starting any job", async () => {
  const paths: string[] = [];
  const result = await resolveVolumeContinuation(novel, chapter, async (path) => {
    paths.push(path);
    if (path.endsWith("volume-writing-job")) return { job: null };
    if (path.startsWith("/api/chapters/")) return { novel_id: novel, volume_id: volume };
    return { _id: volume, novel_id: novel, title: "京门问粮" };
  });
  assert.deepEqual(result, { id: volume, title: "京门问粮" });
  assert.equal(paths.length, 3);
  assert.ok(paths.every((path) => !path.includes("first-chapter-job")));
});

test("an active or paused volume job takes precedence over the first chapter", async () => {
  const paths: string[] = [];
  const result = await resolveVolumeContinuation(novel, chapter, async (path) => {
    paths.push(path);
    return path.endsWith("volume-writing-job")
      ? { job: { active: true, novel_id: novel, volume_id: volume, status: "paused" } }
      : { _id: volume, novel_id: novel, title: "京门问粮" };
  });
  assert.equal(result.id, volume);
  assert.ok(!paths.some((path) => path.startsWith("/api/chapters/")));
});

test("cross-novel or missing targets fail closed without opening a writer", async () => {
  await assert.rejects(resolveVolumeContinuation(novel, chapter, async (path) =>
    path.endsWith("volume-writing-job") ? { job: null } : { novel_id: "another-book", volume_id: volume }));
  await assert.rejects(resolveVolumeContinuation(novel, chapter, async (path) =>
    path.endsWith("volume-writing-job") ? { job: null } : { novel_id: novel, volume_id: null }));
});
