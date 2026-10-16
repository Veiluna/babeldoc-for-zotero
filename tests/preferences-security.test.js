const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const preferencesSource = fs.readFileSync(
  path.join(__dirname, "../addon/content/preferences.js"),
  "utf8"
);

async function testConnection(
  outcome,
  apiKey = "DEMO/SECRET-ONLY",
  baseURL = "https://example.test/v1",
  settingsError = null,
) {
  let testClick;
  const messages = new Map();
  const requests = [];
  const httpLogs = [];
  const pane = { dataset: {}, addEventListener() {} };
  const button = {
    disabled: false,
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
      if (!elements[id]) {
        elements[id] = {
          id,
          removeAttribute() {},
          setAttribute() {},
        };
      }
      return elements[id];
    },
  };
  const config = {
    getSettings() {
      if (settingsError) throw settingsError;
      return { baseURL, apiKey, model: "test-model", babeldocPath: "" };
    },
    chatCompletionsURL(baseURL) {
      if (baseURL.includes("?")) return baseURL;
      return baseURL.replace(/\/+$/, "") + "/chat/completions";
    },
    resolvePythonFromBabelDOCPath() {
      return "";
    },
  };
  class TimeoutException extends Error {}
  const Zotero = {
    HTTP: {
      TimeoutException,
      async request(method, url, options) {
        // Zotero's HTTP layer may log the complete request URL before resolving.
        httpLogs.push("HTTP request " + url);
        requests.push({ method, url, options });
        if (outcome.error) throw outcome.error;
        return outcome.response;
      },
    },
  };
  const window = {};
  const context = { ChromeUtils: {}, Zotero, ZoteroTranslateFilesConfig: config, window };

  vm.runInNewContext(preferencesSource, context, { filename: "preferences.js" });
  window.ZoteroTranslateFilesPreferences.initializePane(document, window);
  await testClick({ currentTarget: button });

  return {
    status: messages.get("zotero-translate-files-preferences-api-status"),
    statusElement: elements["zotero-translate-files-preferences-api-status"],
    request: requests[0],
    httpLogs,
    button,
    TimeoutException,
  };
}

(async () => {
  const apiKey = "DEMO/SECRET-ONLY";
  const escapedJSON = '{"error":{"message":"Invalid key: DEMO\\/SECRET-ONLY"}}';
  assert.ok(escapedJSON.includes("DEMO\\/SECRET-ONLY"), "fixture must contain an escaped slash");
  const jsonCase = await testConnection({ response: { status: 401, responseText: escapedJSON } }, apiKey);
  assert.equal(jsonCase.status.messageID, "zotero-translate-files-pref-api-http-error");
  assert.ok(!jsonCase.status.args.body.includes(apiKey), "decoded JSON must not expose the configured key");
  assert.ok(jsonCase.status.args.body.includes("[已隐藏]"), "decoded JSON should show the redaction marker");

  assert.equal(jsonCase.request.method, "POST");
  assert.equal(jsonCase.request.url, "https://example.test/v1/chat/completions");
  assert.equal(jsonCase.request.options.headers["Content-Type"], "application/json");
  assert.equal(jsonCase.request.options.headers.Authorization, "Bearer " + apiKey);
  assert.equal(jsonCase.request.options.headers["api-key"], apiKey);
  assert.equal(jsonCase.request.options.responseType, "text");
  assert.equal(jsonCase.request.options.successCodes, false);
  assert.equal(jsonCase.request.options.timeout, 30000);
  assert.deepEqual(JSON.parse(jsonCase.request.options.body), {
    model: "test-model",
    messages: [{ role: "user", content: "ping" }],
    max_tokens: 1,
  });

  const numericApiKey = "1234567890";
  const numericJSON = '{"error":{"api_key":1234567890}}';
  const numericCase = await testConnection({ response: { status: 401, responseText: numericJSON } }, numericApiKey);
  assert.ok(!numericCase.status.args.body.includes(numericApiKey), "numeric JSON values must not expose the configured key");
  assert.ok(numericCase.status.args.body.includes("[已隐藏]"), "numeric JSON values should show the redaction marker");

  const plainTextCase = await testConnection({ response: { status: 401, responseText: "Invalid key: DEMO/SECRET-ONLY" } }, apiKey);
  assert.ok(!plainTextCase.status.args.body.includes(apiKey), "plain-text fallback must not expose the configured key");
  assert.ok(plainTextCase.status.args.body.includes("[已隐藏]"), "plain-text fallback should show the redaction marker");

  const statusError = new Error("HTTP request failed");
  statusError.xhr = { status: 429, responseText: "rate limit for DEMO/SECRET-ONLY" };
  const statusErrorCase = await testConnection({ error: statusError }, apiKey);
  assert.equal(statusErrorCase.status.messageID, "zotero-translate-files-pref-api-http-error");
  assert.equal(statusErrorCase.status.args.status, 429);
  assert.ok(!statusErrorCase.status.args.body.includes(apiKey), "exception response bodies must redact the API key");

  const zoteroStatusError = new Error("HTTP request failed");
  zoteroStatusError.xmlhttp = { status: 503, responseText: "server unavailable" };
  const zoteroStatusCase = await testConnection({ error: zoteroStatusError }, apiKey);
  assert.equal(zoteroStatusCase.status.messageID, "zotero-translate-files-pref-api-http-error");
  assert.equal(zoteroStatusCase.status.args.status, 503, "Zotero 10 HTTP errors expose their XHR as xmlhttp");

  const timeoutCase = await testConnection({ error: new jsonCase.TimeoutException("Request timed out") }, apiKey);
  assert.equal(timeoutCase.status.messageID, "zotero-translate-files-pref-api-timeout");
  assert.ok(!JSON.stringify(timeoutCase.status).includes(apiKey), "timeout messages must not expose the API key");

  const networkCase = await testConnection({ error: new Error("network failure containing DEMO/SECRET-ONLY") }, apiKey);
  assert.equal(networkCase.status.messageID, "zotero-translate-files-pref-api-network-error");
  assert.ok(!JSON.stringify(networkCase.status).includes(apiKey), "network messages must not expose the API key");

  const zeroStatusCase = await testConnection({ response: { status: 0, responseText: "" } }, apiKey);
  assert.equal(zeroStatusCase.status.messageID, "zotero-translate-files-pref-api-network-error");
  assert.ok(!JSON.stringify(zeroStatusCase.status).includes(apiKey), "status 0 network messages must not expose the API key");

  const successCase = await testConnection({ response: { status: 204, responseText: "" } }, apiKey);
  assert.equal(successCase.status.messageID, "zotero-translate-files-pref-api-success");
  assert.equal(successCase.status.args.status, 204);

  const invalidSettingsCase = await testConnection(
    { response: { status: 204, responseText: "" } },
    apiKey,
    "https://example.test/v1",
    new Error("单篇最多请求次数必须是大于或等于 1 的整数。"),
  );
  assert.equal(invalidSettingsCase.button.disabled, false, "settings validation must re-enable the test button");
  assert.match(invalidSettingsCase.statusElement.textContent, /单篇最多请求次数必须是大于或等于 1 的整数/);

  const encodedKey = encodeURIComponent(apiKey);
  const encodedKeyLowerHex = encodedKey.replace(/%[0-9A-F]{2}/g, (escape) => escape.toLowerCase());
  const keyBearingURLs = [
    `https://example.test/v1/chat/completions?token=${apiKey}`,
    `https://example.test/v1/chat/completions?token=${encodedKey}`,
    `https://example.test/v1/chat/completions?token=${encodedKeyLowerHex}`,
    `https://example.test/v1/chat/completions?token=${encodeURIComponent(encodedKey)}`,
  ];
  for (const url of keyBearingURLs) {
    const blockedCase = await testConnection({ response: { status: 200, responseText: "{}" } }, apiKey, url);
    assert.equal(
      blockedCase.status.messageID,
      "zotero-translate-files-pref-api-key-in-url",
      "URLs containing the API key must be rejected before making a request",
    );
    assert.equal(blockedCase.request, undefined, "a rejected URL must never reach Zotero.HTTP.request");
    assert.deepEqual(blockedCase.httpLogs, [], "a rejected URL must never reach the HTTP logging boundary");
    assert.ok(!JSON.stringify(blockedCase.status).includes(apiKey), "the rejection message must not expose the API key");
    assert.ok(!JSON.stringify(blockedCase.status).includes(url), "the rejection message must not expose the URL");
  }

  process.stdout.write("preferences HTTP and redaction regression cases passed\n");
})().catch((error) => {
  process.stderr.write(error.stack + "\n");
  process.exitCode = 1;
});
