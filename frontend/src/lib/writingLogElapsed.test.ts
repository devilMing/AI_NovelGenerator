import assert from "node:assert/strict";
import test from "node:test";
import { completedLogElapsedSeconds } from "./writingLogElapsed.ts";

test("unfinished log entries never display a misleading zero duration", () => {
  assert.equal(completedLogElapsedSeconds({ state: "running", elapsed_ms: 0 }), null);
  assert.equal(completedLogElapsedSeconds({ state: "running", elapsed_ms: 1250 }), null);
  assert.equal(completedLogElapsedSeconds({ state: "pending", elapsed_ms: 0 }), null);
});

test("finished entries preserve legitimate zero and elapsed duration", () => {
  assert.equal(completedLogElapsedSeconds({ state: "done", elapsed_ms: 0 }), "0.00");
  assert.equal(completedLogElapsedSeconds({ state: "failed", elapsed_ms: 1250 }), "1.25");
  assert.equal(completedLogElapsedSeconds({ state: "done" }), null);
  assert.equal(completedLogElapsedSeconds({ state: "done", elapsed_ms: Number.NaN }), null);
  assert.equal(completedLogElapsedSeconds({ state: "done", elapsed_ms: -1 }), null);
});
