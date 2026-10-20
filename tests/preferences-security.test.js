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
  tokenParam = "max_tokens",
  installerReady = false,
  invalidMaxRequests = false,
) {
  let testClick;
  let installClick;
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
  const installButton = {
    disabled: false,
    addEventListener(_event, callback) {
      installClick = callback;
    },
  };
  const installerCalls = [];
  let readinessCalls = 0;
  const elements = {
    "babeldoc-for-zotero-preferences": pane,
    "babeldoc-for-zotero-preferences-test": button,
    "babeldoc-for-zotero-preferences-install": installButton,
    // The environment check button only needs a listener during pane init.
    "babeldoc-for-zotero-preferences-check": {
      id: "babeldoc-for-zotero-preferences-check",
      addEventListener() {},
    },
    "babeldoc-for-zotero-preferences-homepage": {
      id: "babeldoc-for-zotero-preferences-homepage",
      addEventListener() {},
    },
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
  // Real token-parameter helpers from config.js, with a recording Prefs stub.
  const prefsSet = [];
  const prefValues = new Map([
    ["extensions.babeldoc-for-zotero.tokenParam", tokenParam],
    ...(invalidMaxRequests ? [["extensions.babeldoc-for-zotero.maxRequests", 0]] : []),
  ]);
  const configContext = {
    Zotero: {
      Prefs: {
        get(key) { return prefValues.has(key) ? prefValues.get(key) : null; },
        set(key, value) {
          prefValues.set(key, value);
          prefsSet.push([key, value]);
        },
      },
    },
  };
  vm.runInNewContext(
    fs.readFileSync(path.join(__dirname, "../addon/content/modules/config.js"), "utf8") +
      "\nthis.ZoteroTranslateFilesConfig = ZoteroTranslateFilesConfig;",
    configContext,
    { filename: "config.js" }
  );
  const realConfig = configContext.ZoteroTranslateFilesConfig;
  const config = {
    getSettings() {
      if (settingsError) throw settingsError;
      return {
        baseURL,
        apiKey,
        model: "test-model",
        downloadSource: "china",
        tokenParam: realConfig.getSettings().tokenParam,
        ...(invalidMaxRequests ? realConfig.getSettings() : {}),
        baseURL, apiKey, model: "test-model",
      };
    },
    getDownloadSource: realConfig.getDownloadSource,
    redact: realConfig.redact,
    tokenLimitAdaptation: realConfig.tokenLimitAdaptation,
    setTokenParam: realConfig.setTokenParam,
    chatCompletionsURL(baseURL) {
      if (baseURL.includes("?")) return baseURL;
      return baseURL.replace(/\/+$/, "") + "/chat/completions";
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
        if (outcome.responses) return outcome.responses[Math.min(requests.length, outcome.responses.length) - 1];
        return outcome.response;
      },
    },
  };
  const window = {};
  const context = {
    ChromeUtils: {},
    Zotero,
    ZoteroTranslateFilesConfig: config,
    ZoteroTranslateFilesInstaller: {
      baseDir() { return "C:\\Users\\test\\AppData\\Local\\babeldoc-for-zotero"; },
      isInstalling() { return false; },
      async isReady() { readinessCalls++; return installerReady; },
      async repairEnvironment(options) {
        installerCalls.push(options);
        if (typeof options.onStart === "function") options.onStart();
        return "C:\\env\\python.exe";
      },
    },
    window,
  };

  vm.runInNewContext(preferencesSource, context, { filename: "preferences.js" });
  window.ZoteroTranslateFilesPreferences.initializePane(document, window);
  await testClick({ currentTarget: button });

  return {
    status: messages.get("babeldoc-for-zotero-preferences-api-status"),
    statusElement: elements["babeldoc-for-zotero-preferences-api-status"],
    request: requests[0],
    requests,
    prefsSet,
    tokenLimitAdaptation: realConfig.tokenLimitAdaptation,
    httpLogs,
    button,
    installButton,
    installClick,
    installerCalls,
    get readinessCalls() { return readinessCalls; },
    TimeoutException,
  };
}

(async () => {
  const apiKey = "DEMO/SECRET-ONLY";
  const escapedJSON = '{"error":{"message":"Invalid key: DEMO\\/SECRET-ONLY"}}';
  assert.ok(escapedJSON.includes("DEMO\\/SECRET-ONLY"), "fixture must contain an escaped slash");
  const jsonCase = await testConnection({ response: { status: 401, responseText: escapedJSON } }, apiKey);
  assert.equal(jsonCase.status.messageID, "babeldoc-for-zotero-pref-api-http-error");
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
    max_tokens: 16,
  });

  // A model that rejects max_tokens gets one reshaped retry with
  // max_completion_tokens, and the working parameter is remembered.
  const unsupported = JSON.stringify({
    error: {
      message: "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.",
      type: "invalid_request_error",
      param: "max_tokens",
      code: "unsupported_parameter",
    },
  });
  const adaptCase = await testConnection({
    responses: [
      { status: 400, responseText: unsupported },
      { status: 200, responseText: '{"choices":[]}' },
    ],
  });
  assert.equal(adaptCase.status.messageID, "babeldoc-for-zotero-pref-api-success");
  assert.equal(adaptCase.requests.length, 2);
  assert.equal(JSON.parse(adaptCase.requests[0].options.body).max_tokens, 16);
  const retryBody = JSON.parse(adaptCase.requests[1].options.body);
  assert.equal(retryBody.max_completion_tokens, 16);
  assert.ok(!("max_tokens" in retryBody));
  assert.deepEqual(adaptCase.prefsSet, [["extensions.babeldoc-for-zotero.tokenParam", "max_completion_tokens"]]);

  // The saved max_completion_tokens dialect can also be switched back when a
  // provider changes routes and rejects it in favor of max_tokens.
  const reverseUnsupported = JSON.stringify({
    error: {
      message: "Unsupported parameter: 'max_completion_tokens' is not supported with this model. Use 'max_tokens' instead.",
      type: "invalid_request_error",
      param: "max_completion_tokens",
      code: "unsupported_parameter",
    },
  });
  const reverseCase = await testConnection({
    responses: [
      { status: 400, responseText: reverseUnsupported },
      { status: 200, responseText: '{"choices":[]}' },
    ],
  }, undefined, undefined, null, "max_completion_tokens");
  assert.equal(reverseCase.status.messageID, "babeldoc-for-zotero-pref-api-success");
  assert.equal(reverseCase.requests.length, 2);
  assert.equal(JSON.parse(reverseCase.requests[0].options.body).max_completion_tokens, 16);
  assert.equal(JSON.parse(reverseCase.requests[1].options.body).max_tokens, 16);
  assert.deepEqual(reverseCase.prefsSet, [["extensions.babeldoc-for-zotero.tokenParam", "max_tokens"]]);

  // When `param` is absent, fallback recognizes the rejected name, not the
  // replacement suggested later in the same response.
  const reverseWithoutParam = JSON.stringify({
    error: { message: "Unsupported parameter: 'max_completion_tokens' is not supported with this model. Use 'max_tokens' instead.", code: "unsupported_parameter" },
  });
  assert.equal(reverseCase.tokenLimitAdaptation(400, reverseWithoutParam), "max_tokens");
  assert.equal(
    reverseCase.tokenLimitAdaptation(400, JSON.stringify({ error: { message: "Unsupported request. Use 'max_tokens' instead of 'max_completion_tokens'.", code: "unsupported_parameter" } })),
    "",
  );

  // Any other 400 is reported as-is, without a retry.
  const otherCase = await testConnection({ responses: [{ status: 400, responseText: '{"error":{"message":"model not found"}}' }] });
  assert.equal(otherCase.requests.length, 1);
  assert.equal(otherCase.status.messageID, "babeldoc-for-zotero-pref-api-http-error");

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
  assert.equal(statusErrorCase.status.messageID, "babeldoc-for-zotero-pref-api-http-error");
  assert.equal(statusErrorCase.status.args.status, 429);
  assert.ok(!statusErrorCase.status.args.body.includes(apiKey), "exception response bodies must redact the API key");

  const zoteroStatusError = new Error("HTTP request failed");
  zoteroStatusError.xmlhttp = { status: 503, responseText: "server unavailable" };
  const zoteroStatusCase = await testConnection({ error: zoteroStatusError }, apiKey);
  assert.equal(zoteroStatusCase.status.messageID, "babeldoc-for-zotero-pref-api-http-error");
  assert.equal(zoteroStatusCase.status.args.status, 503, "Zotero 10 HTTP errors expose their XHR as xmlhttp");

  const timeoutCase = await testConnection({ error: new jsonCase.TimeoutException("Request timed out") }, apiKey);
  assert.equal(timeoutCase.status.messageID, "babeldoc-for-zotero-pref-api-timeout");
  assert.ok(!JSON.stringify(timeoutCase.status).includes(apiKey), "timeout messages must not expose the API key");

  const networkCase = await testConnection({ error: new Error("network failure containing DEMO/SECRET-ONLY") }, apiKey);
  assert.equal(networkCase.status.messageID, "babeldoc-for-zotero-pref-api-network-error");
  assert.ok(!JSON.stringify(networkCase.status).includes(apiKey), "network messages must not expose the API key");

  const zeroStatusCase = await testConnection({ response: { status: 0, responseText: "" } }, apiKey);
  assert.equal(zeroStatusCase.status.messageID, "babeldoc-for-zotero-pref-api-network-error");
  assert.ok(!JSON.stringify(zeroStatusCase.status).includes(apiKey), "status 0 network messages must not expose the API key");

  const successCase = await testConnection({ response: { status: 204, responseText: "" } }, apiKey);
  assert.equal(successCase.status.messageID, "babeldoc-for-zotero-pref-api-success");
  assert.equal(successCase.status.args.status, 204);

  const settingsResumeCase = await testConnection(
    { response: { status: 204, responseText: "" } },
    apiKey,
    "https://example.test/v1",
    null,
    "max_tokens",
    false,
  );
  const resumeReadinessChecks = settingsResumeCase.readinessCalls;
  await settingsResumeCase.installClick({ currentTarget: settingsResumeCase.installButton });
  assert.equal(settingsResumeCase.installerCalls.length, 1);
  assert.equal(typeof settingsResumeCase.installerCalls[0].onStart, "function",
    "settings should start progress only after repair has passed its safety checks");
  assert.equal(Object.hasOwn(settingsResumeCase.installerCalls[0], "force"), false,
    "settings should use the dedicated full-repair path instead of force mode");
  assert.equal(settingsResumeCase.readinessCalls, resumeReadinessChecks,
    "settings should not make a separate readiness check that can race with the shared install");

  const settingsRepairCase = await testConnection(
    { response: { status: 204, responseText: "" } },
    apiKey,
    "https://example.test/v1",
    null,
    "max_tokens",
    true,
  );
  const repairReadinessChecks = settingsRepairCase.readinessCalls;
  await settingsRepairCase.installClick({ currentTarget: settingsRepairCase.installButton });
  assert.equal(typeof settingsRepairCase.installerCalls[0].onStart, "function");
  assert.equal(settingsRepairCase.readinessCalls, repairReadinessChecks);

  const invalidSettingsCase = await testConnection(
    { response: { status: 204, responseText: "" } },
    apiKey,
    "https://example.test/v1",
    new Error("单篇最多请求次数必须是大于或等于 1 的整数。"),
  );
  assert.equal(invalidSettingsCase.button.disabled, false, "settings validation must re-enable the test button");
  assert.match(invalidSettingsCase.statusElement.textContent, /单篇最多请求次数必须是大于或等于 1 的整数/);

  const invalidMaxRequestsInstallCase = await testConnection(
    { response: { status: 204, responseText: "" } },
    apiKey,
    "https://example.test/v1",
    null,
    "max_tokens",
    false,
    true,
  );
  assert.equal(invalidMaxRequestsInstallCase.status.messageID, "babeldoc-for-zotero-pref-api-success",
    "历史请求上限无效值不能再阻止连接测试");
  await invalidMaxRequestsInstallCase.installClick({ currentTarget: invalidMaxRequestsInstallCase.installButton });
  assert.equal(invalidMaxRequestsInstallCase.installerCalls.length, 1,
    "invalid translation settings must not block the settings install button");
  assert.equal(invalidMaxRequestsInstallCase.installerCalls[0].source, "china");
  assert.equal(invalidMaxRequestsInstallCase.installButton.disabled, false);

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
      "babeldoc-for-zotero-pref-api-key-in-url",
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
