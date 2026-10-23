import assert from "node:assert/strict";
import test from "node:test";

import { validateConfig } from "./validation.ts";
import type { AppConfig } from "../types/config.ts";

function configWithDatabase(overrides: Partial<AppConfig> = {}): AppConfig {
  return {
    config_version: 1,
    mongodb_url_configured: false,
    mongodb_url_update: { action: "preserve" },
    mongo_database_name: "",
    mongo_timeout_ms: 5000,
    llm: { default_provider: "", providers: {} },
    ...overrides,
  };
}

test("allows an LLM-only save when MongoDB is supplied by the runtime environment", () => {
  const error = validateConfig(configWithDatabase(), (key) => key);

  assert.equal(error, null);
});

test("requires a database name when replacing the managed MongoDB URL", () => {
  const error = validateConfig(
    configWithDatabase({
      mongodb_url_update: { action: "replace", value: "mongodb://127.0.0.1:27017" },
    }),
    (key) => key,
  );

  assert.equal(error, "database.required: mongo_database_name");
});

