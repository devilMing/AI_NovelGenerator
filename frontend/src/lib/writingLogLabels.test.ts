import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("shared planning logs do not mislabel resource and volume preparation as chapter plans", () => {
  const zh = JSON.parse(readFileSync(new URL("../i18n/messages/zh.json", import.meta.url), "utf8"));
  const en = JSON.parse(readFileSync(new URL("../i18n/messages/en.json", import.meta.url), "utf8"));
  assert.equal(zh.writing.volumeWriting.stage.planning, "准备与规划");
  assert.equal(en.writing.volumeWriting.stage.planning, "Preparation and planning");
});

test("inactive log snapshots do not claim to refresh every two seconds", () => {
  const zh = JSON.parse(readFileSync(new URL("../i18n/messages/zh.json", import.meta.url), "utf8"));
  const en = JSON.parse(readFileSync(new URL("../i18n/messages/en.json", import.meta.url), "utf8"));
  assert.match(zh.writing.volumeWriting.logs.snapshotHint ?? "", /停止自动刷新/);
  assert.match(en.writing.volumeWriting.logs.snapshotHint ?? "", /not being refreshed/);
});

test("automatic chapter fact closure has distinct progress labels", () => {
  const zh = JSON.parse(readFileSync(new URL("../i18n/messages/zh.json", import.meta.url), "utf8"));
  const en = JSON.parse(readFileSync(new URL("../i18n/messages/en.json", import.meta.url), "utf8"));
  assert.deepEqual(
    [zh.writing.volumeWriting.stage.fact_extracting, zh.writing.volumeWriting.stage.fact_reviewing, zh.writing.volumeWriting.stage.fact_saving],
    ["提取章后事实", "独立审核章后事实", "更新事实库"],
  );
  assert.deepEqual(
    [en.writing.volumeWriting.stage.fact_extracting, en.writing.volumeWriting.stage.fact_reviewing, en.writing.volumeWriting.stage.fact_saving],
    ["Extracting chapter facts", "Reviewing chapter facts", "Updating fact library"],
  );
});
