import assert from "node:assert/strict";
import test from "node:test";

import { getChatGPTModelOptions } from "./chatgptReasoning.ts";

test("keeps the configured model selectable before account models are loaded", () => {
  assert.deepEqual(getChatGPTModelOptions("gpt-4o-mini", []), [
    { id: "gpt-4o-mini", name: "gpt-4o-mini" },
  ]);
});

test("deduplicates the configured model against account models", () => {
  assert.deepEqual(
    getChatGPTModelOptions("gpt-5", [
      { id: "gpt-5", name: "GPT-5" },
      { id: "gpt-5-mini", name: "GPT-5 mini" },
      { id: "gpt-5", name: "Duplicate" },
    ]),
    [
      { id: "gpt-5", name: "GPT-5" },
      { id: "gpt-5-mini", name: "GPT-5 mini" },
    ],
  );
});

