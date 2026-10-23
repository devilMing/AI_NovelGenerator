import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const zh = JSON.parse(readFileSync(new URL("../i18n/messages/zh.json", import.meta.url), "utf8"));
const en = JSON.parse(readFileSync(new URL("../i18n/messages/en.json", import.meta.url), "utf8"));
const rootLayout = readFileSync(new URL("../app/layout.tsx", import.meta.url), "utf8");

test("uses AI NG as the navigation brand in every locale", () => {
  assert.equal(zh.nav.title, "AI NG");
  assert.equal(en.nav.title, "AI NG");
});

test("uses AI NG as the browser tab title", () => {
  assert.match(rootLayout, /title:\s*["']AI NG["']/);
});

