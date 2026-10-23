import assert from "node:assert/strict";
import test from "node:test";
import { canStartFirstChapter, refreshLogActivity, timelineNodeState, wallDuration } from "./firstChapterTimeline.ts";

test("completed runs never show a new start form", () => {
  assert.equal(canStartFirstChapter({ status: "completed", active: false }), false);
  assert.equal(canStartFirstChapter({ status: "running", active: true }), false);
  assert.equal(canStartFirstChapter({ status: "cancelled", active: false }), true);
  assert.equal(canStartFirstChapter(null), true);
});

test("inactive attempts cannot appear running, and historical failures remain failures", () => {
  assert.equal(timelineNodeState("running", false), "stopped");
  assert.equal(timelineNodeState("failed", false), "failed");
  assert.equal(timelineNodeState("done", false), "done");
  assert.equal(timelineNodeState(undefined, false), "info");
  assert.equal(timelineNodeState("running", true, false), "stopped");
  assert.equal(timelineNodeState("running", true, true), "running");
});

test("wall duration handles hours and missing values without inventing zero", () => {
  assert.equal(wallDuration(7140000), "1:59:00");
  assert.equal(wallDuration(0), "0:00");
  assert.equal(wallDuration(null), null);
  assert.equal(wallDuration(-1), null);
});

test("new attempt invalidates old live flags even outside the refreshed log page", () => {
  const cached = [{ id: "outside-latest-50", attempt_key: "old", is_live_attempt: true },
    { id: "new", attempt_key: "current", is_live_attempt: true }];
  assert.deepEqual(refreshLogActivity(cached, "current").map((row) => row.is_live_attempt), [false, true]);
  assert.deepEqual(refreshLogActivity(cached, null).map((row) => row.is_live_attempt), [false, false]);
  assert.equal(cached[0].is_live_attempt, true, "cached inputs must not be mutated");
});

test("volume logs without attempt metadata retain their existing behavior", () => {
  const cached = [{ id: "volume-log", is_live_attempt: true }];
  assert.equal(refreshLogActivity(cached, undefined), cached);
});
