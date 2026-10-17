const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const bootstrapSource = fs.readFileSync(
  path.join(__dirname, "../addon/bootstrap.js"),
  "utf8",
);

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function makeHarness({ ready = false, pendingInstall = false, installError = null, progressValue = null,
  invalidSettings = false,
  shutdownOnProgressShow = false } = {}) {
  const installCalls = [];
  const progressWindows = [];
  const debugLogs = [];
  let cancelCalls = 0;
  let activateCalls = 0;
  let settingsCalls = 0;
  let sourceCalls = 0;
  const uiReady = deferred();
  const installResult = deferred();
  const mainWindow = {
    closed: false,
    MozXULElement: { insertFTLIfNeeded() {} },
    document: {
      querySelector() { return null; },
      l10n: {
        async formatValue(messageID, args) {
          if (messageID === "babeldoc-for-zotero-startup-progress-stage") {
            return `Preparing translation environment (step ${args.stepIndex}/${args.stepCount}): ${args.step}`;
          }
          return messageID;
        },
      },
    },
  };
  const addon = {
    init() {},
    addToWindow() {},
    removeFromWindow() {},
    shutdown() {},
  };
  const installer = {
    async isReady() { return ready; },
    activate() { activateCalls++; },
    createInstallAbortController() { return new AbortController(); },
    cancelInFlight() { cancelCalls++; },
    ensureReady(options) {
      installCalls.push(options);
      if (progressValue && options.onProgress) options.onProgress(progressValue);
      if (installError) return Promise.reject(installError);
      if (!pendingInstall) return Promise.resolve("python.exe");
      return new Promise((resolve, reject) => {
        if (options.signal.aborted) {
          reject(new Error("cancelled"));
          return;
        }
        options.signal.addEventListener("abort", () => reject(new Error("cancelled")), { once: true });
        installResult.promise.then(resolve, reject);
      });
    },
  };
  let context;
  context = {
    Zotero: {
      initializationPromise: Promise.resolve(),
      uiReadyPromise: uiReady.promise,
      getMainWindows() { return [mainWindow]; },
      getMainWindow() { return mainWindow; },
      debug(message) { debugLogs.push(String(message)); },
      ProgressWindow: function ProgressWindow(options) {
        const entry = {
          options,
          headline: "",
          shown: false,
          closed: false,
          closeTimer: null,
          item: { progress: 0, text: "", error: false },
        };
        progressWindows.push(entry);
        this.changeHeadline = headline => { entry.headline = headline; };
        this.ItemProgress = function ItemProgress(icon, label) {
          entry.icon = icon;
          entry.label = label;
          return {
            setProgress(value) { entry.item.progress = value; },
            setText(value) { entry.item.text = value; },
            setError() { entry.item.error = true; },
          };
        };
        this.show = () => {
          entry.shown = true;
          if (shutdownOnProgressShow) context.shutdown();
        };
        this.startCloseTimer = milliseconds => { entry.closeTimer = milliseconds; };
        this.close = () => { entry.closed = true; };
      },
    },
    ZoteroTranslateFilesInstaller: installer,
    ZoteroTranslateFilesConfig: {
      getSettings() {
        settingsCalls++;
        if (invalidSettings) throw new Error("maxRequests must be at least 1");
        return { downloadSource: "china" };
      },
      getDownloadSource() { sourceCalls++; return "china"; },
      redact(value) { return String(value).replaceAll("STARTUP-SECRET", "[REDACTED]"); },
    },
    Services: {
      scriptloader: {
        loadSubScript(scriptPath) {
          if (scriptPath.endsWith("content/main.js")) context.ZoteroTranslateFiles = addon;
        },
      },
    },
  };
  vm.runInNewContext(bootstrapSource, context, { filename: "bootstrap.js" });
  uiReady.resolve();
  return {
    context,
    installCalls,
    progressWindows,
    debugLogs,
    get settingsCalls() { return settingsCalls; },
    get sourceCalls() { return sourceCalls; },
    get activateCalls() { return activateCalls; },
    get cancelCalls() { return cancelCalls; },
    installResult,
    async start() {
      await context.startup({ id: "babeldoc-for-zotero", version: "0.4.0", rootURI: "resource://babeldoc/" });
      await new Promise(resolve => setImmediate(resolve));
    },
  };
}

async function startupInstallsOnlyWhenNotReady() {
  const readyHarness = makeHarness({ ready: true });
  await readyHarness.start();
  assert.equal(readyHarness.installCalls.length, 0, "startup must skip installation when the environment is ready");
  assert.equal(readyHarness.activateCalls, 1, "startup should reactivate the installer lifecycle");
  assert.equal(readyHarness.progressWindows.length, 0);

  const missingHarness = makeHarness({ progressValue: {
    overall: 20,
    stepId: "download-uv",
    stepIndex: 1,
    stepCount: 5,
  } });
  await missingHarness.start();
  assert.equal(missingHarness.installCalls.length, 1, "startup should make one background install attempt");
  assert.equal(missingHarness.installCalls[0].interactive, false);
  assert.equal(missingHarness.installCalls[0].source, "china");
  assert.equal(missingHarness.progressWindows.length, 1);
  assert.equal(missingHarness.progressWindows[0].shown, true);
  assert.equal(missingHarness.progressWindows[0].item.text, "babeldoc-for-zotero-startup-ready",
    "an asynchronously localized progress update must not overwrite the completion notice");

  const invalidSettingsHarness = makeHarness({ invalidSettings: true });
  await invalidSettingsHarness.start();
  assert.equal(invalidSettingsHarness.installCalls.length, 1,
    "an invalid maxRequests preference must not prevent startup from installing the environment");
  assert.equal(invalidSettingsHarness.installCalls[0].source, "china");
  assert.equal(invalidSettingsHarness.settingsCalls, 0,
    "startup should not validate unrelated translation preferences");
  assert.equal(invalidSettingsHarness.sourceCalls, 1,
    "startup should read only the normalized download source");
}

async function shutdownDuringProgressWindowHandoffClosesTheWindow() {
  const harness = makeHarness({ shutdownOnProgressShow: true });
  await harness.start();

  assert.equal(harness.progressWindows.length, 1);
  assert.equal(harness.progressWindows[0].shown, true);
  assert.equal(harness.progressWindows[0].closed, true,
    "shutdown between showing and assigning the progress window must close it");
  assert.equal(harness.installCalls.length, 0,
    "the stale startup continuation must not start installation after shutdown");
}

async function shutdownAbortsBackgroundInstallAndSuppressesNotice() {
  const harness = makeHarness({ pendingInstall: true });
  await harness.start();
  const progress = harness.progressWindows[0];
  const signal = harness.installCalls[0].signal;

  harness.context.shutdown();
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(signal.aborted, true, "shutdown should cancel the startup install waiter");
  assert.equal(harness.cancelCalls, 1, "shutdown should cancel any shared settings or translation install too");
  assert.equal(progress.closed, true, "shutdown should close the startup progress window");
  assert.equal(progress.item.error, false, "shutdown must not show the background failure notice");
  assert.equal(progress.closeTimer, null);
}

async function startupErrorLogsAreRedacted() {
  const harness = makeHarness({ installError: Object.assign(
    new Error("failed with STARTUP-SECRET"),
    { logPath: "C:\\logs\\install.log" },
  ) });
  await harness.start();
  await new Promise(resolve => setImmediate(resolve));

  const failureLog = harness.debugLogs.find(message => message.includes("startup install failed:"));
  assert.ok(failureLog);
  assert.equal(failureLog.includes("STARTUP-SECRET"), false);
  assert.equal(failureLog.includes("[REDACTED]"), true);
  assert.equal(harness.progressWindows[0].closeTimer, 8000);

  const failedRedactionHarness = makeHarness({ installError: Object.assign(
    new Error("failed with STARTUP-SECRET"),
    { logPath: "C:\\logs\\install.log" },
  ) });
  failedRedactionHarness.context.ZoteroTranslateFilesConfig.redact = function () {
    throw new Error("STARTUP-SECRET");
  };
  await failedRedactionHarness.start();
  await new Promise(resolve => setImmediate(resolve));
  const safeFailureLog = failedRedactionHarness.debugLogs.find(message => message.includes("startup install failed:"));
  assert.ok(safeFailureLog);
  assert.equal(safeFailureLog.includes("STARTUP-SECRET"), false);
  assert.equal(safeFailureLog.includes("error details unavailable"), true);
}

(async () => {
  await startupInstallsOnlyWhenNotReady();
  await shutdownDuringProgressWindowHandoffClosesTheWindow();
  await shutdownAbortsBackgroundInstallAndSuppressesNotice();
  await startupErrorLogsAreRedacted();
  process.stdout.write("bootstrap startup install regression cases passed\n");
})().catch(error => {
  process.stderr.write((error && error.stack) || String(error));
  process.exitCode = 1;
});
