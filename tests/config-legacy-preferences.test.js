const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const repositoryRoot = path.resolve(__dirname, "..");
const configSource = fs.readFileSync(
  path.join(repositoryRoot, "addon/content/modules/config.js"),
  "utf8",
);

function loadConfig(initialValues = {}, initialUserValues = []) {
  const values = new Map(Object.entries(initialValues));
  const userValues = new Set(initialUserValues);
  const writes = [];
  const logs = [];
  const context = {
    Zotero: {
      Prefs: {
        get(key, global) {
          assert.equal(global, true);
          return values.get(key);
        },
        set(key, value, global) {
          assert.equal(global, true);
          values.set(key, value);
          userValues.add(key);
          writes.push([key, value]);
        },
        prefHasUserValue(key, global) {
          assert.equal(global, true);
          return userValues.has(key);
        },
      },
      debug(message) {
        logs.push(message);
      },
    },
  };

  vm.runInNewContext(
    configSource + "\nthis.ZoteroTranslateFilesConfig = ZoteroTranslateFilesConfig;",
    context,
    { filename: "config.js" },
  );
  return { config: context.ZoteroTranslateFilesConfig, values, userValues, writes, logs };
}

const newPrefix = "extensions.babeldoc-for-zotero.";
const oldPrefix = "extensions.zotero-translate-files.";

{
  const secret = "private-migration-api-key";
  const oldValues = {
    [oldPrefix + "baseURL"]: "https://example.test/v1",
    [oldPrefix + "apiKey"]: secret,
    [oldPrefix + "model"]: "test-model",
    [oldPrefix + "maxOutputTokens"]: 4096,
    [oldPrefix + "maxRequests"]: 12,
    [oldPrefix + "repairFailed"]: false,
    [oldPrefix + "translateReferences"]: true,
    [oldPrefix + "tokenParam"]: "max_completion_tokens",
  };
  const currentDefaults = {
    [newPrefix + "baseURL"]: "",
    [newPrefix + "apiKey"]: "",
    [newPrefix + "model"]: "",
    [newPrefix + "downloadSource"]: "china",
    [newPrefix + "maxOutputTokens"]: 8192,
    [newPrefix + "maxRequests"]: 20,
    [newPrefix + "repairFailed"]: true,
    [newPrefix + "translateReferences"]: false,
    [newPrefix + "tokenParam"]: "max_tokens",
  };
  const state = loadConfig(
    { ...oldValues, ...currentDefaults },
    Object.keys(oldValues),
  );

  assert.equal(typeof state.config.migrateLegacyPreferences, "function");
  assert.equal(state.config.migrateLegacyPreferences(), 8);
  for (const name of [
    "baseURL",
    "apiKey",
    "model",
    "maxOutputTokens",
    "maxRequests",
    "repairFailed",
    "translateReferences",
    "tokenParam",
  ]) {
    assert.equal(state.values.get(newPrefix + name), oldValues[oldPrefix + name]);
  }
  assert.equal(state.values.get(newPrefix + "legacyPrefsMigrated"), true);
  assert.equal(state.userValues.has(newPrefix + "legacyPrefsMigrated"), true);
  assert.equal(state.logs.length, 1);
  assert.match(state.logs[0], /8/);
  assert.equal(state.values.has(newPrefix + "babeldocPath"), false, "the removed path preference is not migrated");
  assert.equal(state.logs.join("\n").includes(secret), false);
}

{
  const state = loadConfig(
    {
      [oldPrefix + "apiKey"]: "old-secret",
      [newPrefix + "apiKey"]: "new-secret",
    },
    [oldPrefix + "apiKey", newPrefix + "apiKey"],
  );

  assert.equal(state.config.migrateLegacyPreferences(), 0);
  assert.equal(state.values.get(newPrefix + "apiKey"), "new-secret");
  assert.equal(state.values.get(newPrefix + "legacyPrefsMigrated"), true);
}

{
  const state = loadConfig(
    { [oldPrefix + "apiKey"]: "old-secret" },
    [oldPrefix + "apiKey"],
  );
  assert.equal(state.config.migrateLegacyPreferences(), 1);
  state.values.delete(newPrefix + "apiKey");
  state.userValues.delete(newPrefix + "apiKey");
  state.values.set(oldPrefix + "apiKey", "changed-old-secret");
  const writeCountBefore = state.writes.length;

  assert.equal(state.config.migrateLegacyPreferences(), 0);
  assert.equal(state.writes.length, writeCountBefore);
  assert.equal(state.values.has(newPrefix + "apiKey"), false);
}

{
  const state = loadConfig();

  assert.equal(state.config.migrateLegacyPreferences(), 0);
  assert.equal(state.values.get(newPrefix + "legacyPrefsMigrated"), true);
}

{
  const mainSource = fs.readFileSync(path.join(repositoryRoot, "addon/content/main.js"), "utf8");
  const initialization = [];
  const module = (name) => ({
    init() {
      initialization.push(name);
    },
  });
  const context = {
    Zotero: { debug() {} },
    ZoteroTranslateFilesConfig: {
      migrateLegacyPreferences() {
        initialization.push("migration");
      },
    },
    ZoteroTranslateFilesPreferences: module("preferences"),
    ZoteroTranslateFilesMenu: module("menu"),
    ZoteroTranslateFilesQueue: module("queue"),
    ZoteroTranslateFilesRunner: module("runner"),
    ZoteroTranslateFilesImporter: module("importer"),
  };

  vm.runInNewContext(
    mainSource + "\nthis.plugin = ZoteroTranslateFiles;",
    context,
    { filename: "main.js" },
  );
  context.plugin.init({ id: "babeldoc-for-zotero@veiluna.github.io", version: "0.2.6", rootURI: "" });

  assert.deepEqual(initialization, ["migration", "preferences", "menu", "queue", "runner", "importer"]);
}

process.stdout.write("legacy preference migration regression cases passed\n");
