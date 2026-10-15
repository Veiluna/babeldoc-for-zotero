const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const preferencesSource = fs.readFileSync(
  path.join(__dirname, "../addon/content/preferences.js"),
  "utf8"
);

async function displayedErrorBody(body, apiKey) {
  let testClick;
  const messages = new Map();
  const pane = { dataset: {}, addEventListener() {} };
  const button = {
    addEventListener(_event, callback) {
      testClick = callback;
    },
  };
  const elements = {
    "zotero-translate-files-preferences": pane,
    "zotero-translate-files-preferences-browse": { addEventListener() {} },
    "zotero-translate-files-preferences-test": button,
  };
  const document = {
    l10n: {
      setAttributes(element, messageID, args) {
        messages.set(element.id, { messageID, args });
      },
    },
    getElementById(id) {
      if (!elements[id]) elements[id] = { id };
      return elements[id];
    },
  };
  const config = {
    getSettings() {
      return { baseURL: "https://example.test/v1", apiKey, model: "test-model", babeldocPath: "" };
    },
    chatCompletionsURL(baseURL) {
      return baseURL + "/chat/completions";
    },
    openAIBaseURLForBabelDOC(baseURL) {
      return baseURL;
    },
  };
  const window = {
    AbortController,
    clearTimeout,
    fetch: async () => ({ status: 401, text: async () => body }),
    setTimeout,
  };
  const context = { ChromeUtils: {}, Zotero: {}, ZoteroTranslateFilesConfig: config, window };

  vm.runInNewContext(preferencesSource, context, { filename: "preferences.js" });
  window.ZoteroTranslateFilesPreferences.initializePane(document, window);
  await testClick({ currentTarget: button });

  const status = messages.get("zotero-translate-files-preferences-api-status");
  assert.equal(status.messageID, "zotero-translate-files-pref-api-http-error");
  return status.args.body;
}

(async () => {
  const apiKey = "DEMO/SECRET-ONLY";
  const escapedJSON = '{"error":{"message":"Invalid key: DEMO\\/SECRET-ONLY"}}';
  assert.ok(escapedJSON.includes("DEMO\\/SECRET-ONLY"), "fixture must contain an escaped slash");
  const jsonMessage = await displayedErrorBody(escapedJSON, apiKey);
  assert.ok(!jsonMessage.includes(apiKey), "decoded JSON must not expose the configured key");
  assert.ok(jsonMessage.includes("[已隐藏]"), "decoded JSON should show the redaction marker");

  const numericApiKey = "1234567890";
  const numericJSON = '{"error":{"api_key":1234567890}}';
  const numericMessage = await displayedErrorBody(numericJSON, numericApiKey);
  assert.ok(!numericMessage.includes(numericApiKey), "numeric JSON values must not expose the configured key");
  assert.ok(numericMessage.includes("[已隐藏]"), "numeric JSON values should show the redaction marker");

  const plainText = await displayedErrorBody("Invalid key: DEMO/SECRET-ONLY", apiKey);
  assert.ok(!plainText.includes(apiKey), "plain-text fallback must not expose the configured key");
  assert.ok(plainText.includes("[已隐藏]"), "plain-text fallback should show the redaction marker");

  process.stdout.write("preferences security regression cases passed\n");
})().catch((error) => {
  process.stderr.write(error.stack + "\n");
  process.exitCode = 1;
});
