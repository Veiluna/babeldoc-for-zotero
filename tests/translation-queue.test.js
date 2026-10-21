const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../addon/content/modules/queue.js"),
  "utf8"
);
const configSource = fs.readFileSync(
  path.join(__dirname, "../addon/content/modules/config.js"),
  "utf8"
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

function makeHarness(options = {}) {
  const notices = [];
  const progressWindows = [];
  const imports = [];
  const failures = [];
  const logs = [];
  const alerts = [];
  const scheduledCallbacks = [];
  const timers = new Map();
  let timerID = 0;
  let now = 0;
  const mainWindow = { name: "most-recent-zotero-window" };
  const progressWindowSet = { add() {} };
  if (options.formatValue) {
    mainWindow.document = { l10n: { formatValue: options.formatValue } };
    mainWindow.MozXULElement = { insertFTLIfNeeded() {} };
  }
  const items = new Map();
  const settings = options.settings || {
    baseURL: "https://example.test/v1",
    apiKey: "key",
    model: "model",
  };
  const context = {
    ZoteroTranslateFiles: { log: message => logs.push(String(message)) },
    ZoteroTranslateFilesAttachments: {
      isTranslatablePDF: item => !!item && item.translatable !== false,
    },
    Services: {
      prompt: {
        alert(parent, title, message) { alerts.push({ parent, title, message }); },
      },
    },
    setTimeout(callback, delay) {
      const id = ++timerID;
      if (delay === 0) scheduledCallbacks.push({ callback, delay });
      else timers.set(id, { callback, at: now + delay });
      return id;
    },
    clearTimeout: id => timers.delete(id),
    Date: class extends Date { static now() { return now; } },
    Zotero: {
      Prefs: {
        get(name) {
          const prefix = "extensions.babeldoc-for-zotero.";
          return settings[name.slice(prefix.length)];
        },
      },
      Items: { get: id => items.get(id) },
      getMainWindow: () => mainWindow,
      ProgressWindowSet: progressWindowSet,
      ProgressWindow: function (settings = {}) {
        const notice = { descriptions: [], texts: [], shown: false };
        const listeners = new Set();
        notice.domWindow = {
          closed: false,
          addEventListener(type, listener) {
            assert.equal(type, "unload");
            listeners.add(listener);
          },
          removeEventListener(type, listener) {
            assert.equal(type, "unload");
            listeners.delete(listener);
          },
          close() {
            this.closed = true;
            notice.closed = true;
            for (const listener of [...listeners]) listener({ type: "unload", target: this });
          },
          get unloadListenerCount() { return listeners.size; },
        };
        if (settings.closeOnClick === false) progressWindows.push(notice);
        else notices.push(notice);
        this.changeHeadline = () => {};
        this.addDescription = value => notice.descriptions.push(value);
        this.show = () => {
          notice.shown = true;
          progressWindowSet.add(notice.domWindow, this);
          if (options.failProgressShow && settings.closeOnClick === false) throw new Error("show failed");
        };
        this.close = () => {
          notice.publicCloseCalls = (notice.publicCloseCalls || 0) + 1;
          notice.domWindow.close();
        };
        notice.close = () => this.close();
        this.startCloseTimer = (...args) => { notice.closeTimerArgs = args; };
        this.ItemProgress = function (...args) {
          notice.itemProgressArgs = args;
          this.setProgress = value => { notice.progress = value; };
          this.setText = value => { notice.text = value; notice.texts.push(value); };
          this.setError = () => { notice.error = true; };
        };
      },
      debug: message => logs.push(String(message)),
    },
    ZoteroTranslateFilesRunner: options.runner || { translate: async () => "output.pdf" },
  };
  vm.runInNewContext(configSource, context, { filename: "config.js" });
  if (options.useAbortController !== false) context.AbortController = AbortController;
  vm.runInNewContext(source, context, { filename: "queue.js" });

  const queue = context.ZoteroTranslateFilesQueue;
  queue.setImporter(options.importer || {
    importResult: async (item, outputPath) => {
      imports.push([item.id, outputPath]);
      const title = typeof item.getField === "function" ? item.getField("title") : "PDF";
      return { getField: () => String(title || "PDF") + "（中文译本）" };
    },
  });
  const originalFailureHandler = queue.onJobFailed;
  queue.onJobFailed = (job, error) => {
    failures.push({ id: job.attachmentID, message: String(error.message || error) });
    originalFailureHandler(job, error);
  };
  queue.init();

  return {
    queue,
    items,
    failures,
    logs,
    notices,
    progressWindows,
    imports,
    settings,
    alerts,
    mainWindow,
    progressWindowSet,
    get activeTimers() { return timers.size; },
    async advanceTime(milliseconds) {
      const target = now + milliseconds;
      while (true) {
        const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
        if (!next || next[1].at > target) break;
        now = next[1].at;
        timers.delete(next[0]);
        next[1].callback();
        await settleLocalization();
      }
      now = target;
      await settleLocalization();
    },
    flushAlerts() {
      while (scheduledCallbacks.length) {
        const scheduled = scheduledCallbacks.shift();
        assert.equal(scheduled.delay, 0, "failure alerts should be deferred without a delay");
        scheduled.callback();
      }
    },
  };
}

async function settleLocalization() {
  for (let i = 0; i < 8; i++) await Promise.resolve();
}

async function nativePopupUnloadStopsTimersAndInvalidatesLateLocalization() {
  const finish = deferred();
  const lateLabels = [];
  let reportProgress;
  const harness = makeHarness({
    formatValue() {
      const label = deferred();
      lateLabels.push(label);
      return label.promise;
    },
    runner: { translate(_item, { onProgress }) { reportProgress = onProgress; return finish.promise; } },
  });
  const originalAdd = harness.progressWindowSet.add;
  harness.items.set(1, { id: 1 });
  harness.queue.enqueue([1]);
  assert.equal(harness.progressWindowSet.add, originalAdd, "restore registration after show");
  await harness.advanceTime(1000);
  assert.equal(harness.activeTimers, 1);
  const window = harness.progressWindows[0];
  const textsBeforeClose = window.texts.length;
  // Close the native DOM popup directly: never forward through ProgressWindow.close().
  window.domWindow.close();
  assert.equal(window.publicCloseCalls || 0, 0);
  assert.equal(harness.activeTimers, 0, "native popup unload must immediately release its timer");
  assert.equal(window.domWindow.unloadListenerCount, 0, "remove the owned unload listener");
  lateLabels.forEach(label => label.resolve("LATE AFTER NATIVE CLOSE"));
  await settleLocalization();
  reportProgress({ stage: "Generate PDF", overall: 80 });
  await harness.advanceTime(3000);
  assert.equal(harness.activeTimers, 0, "native close must not allow the timer to reschedule");
  assert.equal(window.texts.length, textsBeforeClose, "late labels and progress must not touch a destroyed popup");
  finish.resolve("translated.pdf");
  await harness.queue.onIdle();
  assert.equal(harness.activeTimers, 0);
  assert.equal(harness.imports.length, 1, "closing UI must not cancel translation or import");
}

async function failedShowRestoresRegistrationAndRemovesUnloadListener() {
  const harness = makeHarness({ failProgressShow: true });
  const originalAdd = harness.progressWindowSet.add;
  harness.items.set(1, { id: 1 });
  harness.queue.enqueue([1]);
  await harness.queue.onIdle();
  assert.equal(harness.progressWindowSet.add, originalAdd, "restore registration even when show throws");
  assert.equal(harness.progressWindows[0].domWindow.unloadListenerCount, 0);
  assert.equal(harness.activeTimers, 0);
  assert.equal(harness.imports.length, 1, "a progress UI failure must not stop translation");
}

async function silentStagesKeepShowingRealElapsedTime() {
  const finish = deferred();
  let reportProgress;
  const harness = makeHarness({ runner: {
    translate(_item, { onProgress }) {
      reportProgress = onProgress;
      onProgress({ overall: 39, stage: "Translate Paragraphs", requests: 1, planned: 1 });
      return finish.promise;
    },
  } });
  const originalHook = harness.queue.onProgress;
  let progressEvents = 0;
  harness.queue.onProgress = (job, progress) => {
    progressEvents++;
    originalHook(job, progress);
  };
  harness.items.set(1, { id: 1 });
  harness.queue.enqueue([1]);
  const window = harness.progressWindows[0];
  const before = window.texts.length;
  await harness.advanceTime(3000);
  assert.ok(window.texts.length >= before + 3, "silent requests must refresh text every second");
  for (const elapsed of ["0:01", "0:02", "0:03"]) {
    assert.ok(window.texts.some(text => text.includes(elapsed)), "show real elapsed time " + elapsed);
  }
  assert.equal(window.progress, 39, "the timer must not advance real percentage");
  assert.match(window.text, /Translate Paragraphs · 39% · 请求 1\/1/);
  assert.equal(progressEvents, 1, "UI ticks must not fabricate public progress events");

  reportProgress({ overall: 40, stage: "Translate Paragraphs", requests: 2, planned: 3 });
  harness.queue.enqueue([1]);
  await harness.advanceTime(1000);
  assert.match(window.text, /0:04/, "same-stage progress and queue updates must retain elapsed time");
  assert.match(window.text, /40% · 请求 2\/3/);
  await harness.advanceTime(60000);
  assert.match(window.text, /1:04/);
  assert.equal(window.progress, 40, "a long wait must not invent progress or a timeout failure");
  assert.equal(harness.failures.length, 0);
  reportProgress({ overall: 80, stage: "Generate PDF", requests: 2, planned: 3 });
  await settleLocalization();
  assert.match(window.text, /0:00/, "a new stage resets elapsed time");
  await harness.advanceTime(1000);
  assert.match(window.text, /Generate PDF · 80%/);
  assert.match(window.text, /0:01/);
  finish.resolve("translated.pdf");
  await harness.queue.onIdle();
  assert.equal(harness.activeTimers, 0);
}

async function stageTimersStayWithTheirJobAndStopOnTerminalPaths() {
  for (const outcome of ["success", "failure", "cancelAll", "shutdown", "close"]) {
    const first = deferred();
    const second = deferred();
    const secondStarted = deferred();
    const harness = makeHarness({ runner: {
      translate(item, { onProgress }) {
        onProgress({ overall: 39, stage: "Translate Paragraphs", requests: 1, planned: 1 });
        if (item.id === 1) return first.promise;
        secondStarted.resolve();
        return second.promise;
      },
    } });
    harness.items.set(1, { id: 1 });
    harness.items.set(2, { id: 2 });
    const failureTimerCounts = [];
    const onJobFailed = harness.queue.onJobFailed;
    harness.queue.onJobFailed = (job, error) => {
      failureTimerCounts.push(harness.activeTimers);
      onJobFailed(job, error);
    };
    harness.queue.enqueue([1, 2]);
    await harness.advanceTime(2000);
    const firstWindow = harness.progressWindows[0];
    if (outcome === "cancelAll" || outcome === "shutdown") {
      harness.queue[outcome]();
      assert.equal(harness.activeTimers, 0, outcome + " clears immediately even before runner settles");
    } else if (outcome === "close") {
      firstWindow.close();
      assert.equal(harness.activeTimers, 0, "destroying the progress window clears its timer");
    }
    if (outcome === "failure") first.reject(new Error("provider failed"));
    else first.resolve("first.pdf");
    if (outcome === "cancelAll" || outcome === "shutdown") {
      await harness.queue.onIdle();
      assert.equal(harness.progressWindows.length, 1);
    } else {
      await secondStarted.promise;
      await settleLocalization();
      assert.equal(harness.activeTimers, 1, "only the next job owns a timer after " + outcome);
      assert.match(harness.progressWindows[1].text, /0:00/, "same stage in the next job starts fresh");
      const firstTexts = firstWindow.texts.length;
      await harness.advanceTime(1000);
      assert.equal(firstWindow.texts.length, firstTexts, "old timer must not touch its completed window");
      assert.match(harness.progressWindows[1].text, /0:01/);
      second.resolve("second.pdf");
      await harness.queue.onIdle();
    }
    assert.equal(harness.activeTimers, 0, outcome + " must release timers");
    assert.ok(harness.progressWindows.every(window => window.domWindow.unloadListenerCount === 0),
      outcome + " must remove popup unload listeners");
    assert.deepEqual(failureTimerCounts, outcome === "failure" ? [0] : [],
      "stop before notifying the failure hook");
    const counts = harness.progressWindows.map(window => window.texts.length);
    await harness.advanceTime(5000);
    assert.deepEqual(harness.progressWindows.map(window => window.texts.length), counts);
  }
}

async function elapsedLocalizationCannotOverwriteNewerOrFinishedProgress() {
  const pendingLabels = [];
  const finish = deferred();
  const nextFinish = deferred();
  const nextStarted = deferred();
  let reportProgress;
  const harness = makeHarness({
    formatValue(id, args) {
      assert.equal(id, "babeldoc-for-zotero-progress-stage-elapsed");
      const label = deferred();
      pendingLabels.push({ ...label, args });
      return label.promise;
    },
    runner: { translate(item, { onProgress }) {
      if (item.id === 2) { nextStarted.resolve(); return nextFinish.promise; }
      reportProgress = onProgress;
      return finish.promise;
    } },
  });
  harness.items.set(1, { id: 1 });
  harness.items.set(2, { id: 2 });
  harness.queue.enqueue([1, 2]);
  await harness.advanceTime(1000);
  const oldLabels = pendingLabels.splice(0);
  reportProgress({ stage: "Generate PDF", overall: 80 });
  const current = pendingLabels.pop();
  assert.equal(current.args.elapsed, "0:00");
  current.resolve("本阶段已用时 0:00");
  await settleLocalization();
  const window = harness.progressWindows[0];
  assert.match(window.text, /Generate PDF · 80%.*本阶段已用时 0:00/);
  oldLabels.forEach(label => label.resolve("STALE"));
  await settleLocalization();
  assert.doesNotMatch(window.text, /STALE/);
  await harness.advanceTime(1000);
  const lateLabels = pendingLabels.splice(0);
  finish.resolve("translated.pdf");
  await nextStarted.promise;
  await settleLocalization();
  const completedText = window.text;
  const nextWindow = harness.progressWindows[1];
  const nextText = nextWindow.text;
  lateLabels.forEach(label => label.resolve("LATE"));
  await settleLocalization();
  assert.equal(window.text, completedText, "late localization must not replace completion");
  assert.equal(nextWindow.text, nextText, "old-job localization must not write to the next window");
  nextFinish.resolve("next.pdf");
  await harness.queue.onIdle();
  pendingLabels.forEach(label => label.resolve("AFTER COMPLETION"));
  await settleLocalization();
  assert.match(nextWindow.text, /^已完成/);
  assert.equal(harness.activeTimers, 0);
}

async function timersStopBeforeAsyncResultCleanupCompletes() {
  const cleanupStarted = deferred();
  const cleanupFinished = deferred();
  const harness = makeHarness({ runner: { translate: async () => ({
    outputPath: "translated.pdf",
    cleanup() { cleanupStarted.resolve(); return cleanupFinished.promise; },
  }) } });
  harness.items.set(1, { id: 1 });
  harness.queue.enqueue([1]);
  await cleanupStarted.promise;
  assert.equal(harness.activeTimers, 0, "completed translation must stop ticking before cleanup finishes");
  const count = harness.progressWindows[0].texts.length;
  await harness.advanceTime(3000);
  assert.equal(harness.progressWindows[0].texts.length, count);
  cleanupFinished.resolve();
  await harness.queue.onIdle();
}

async function localizationErrorsFallBackWithoutStoppingTheQueue() {
  for (const formatValue of [() => { throw new Error("private diagnostic"); },
    () => Promise.reject(new Error("private diagnostic")), () => ""]) {
    const finish = deferred();
    const harness = makeHarness({ formatValue, runner: { translate: () => finish.promise } });
    harness.items.set(1, { id: 1 });
    harness.queue.enqueue([1]);
    await harness.advanceTime(1000);
    assert.match(harness.progressWindows[0].text, /Stage elapsed: 0:01/);
    assert.ok(!harness.logs.join("\n").includes("private diagnostic"));
    finish.resolve("translated.pdf");
    await harness.queue.onIdle();
    assert.equal(harness.activeTimers, 0);
  }
}

async function fifoDedupeAndContinueAfterFailure() {
  const firstJob = deferred();
  const started = [];
  const failures = [];
  let active = 0;
  let maxActive = 0;
  const harness = makeHarness({
    runner: {
      translate(item) {
        started.push(item.id);
        active++;
        maxActive = Math.max(maxActive, active);
        const result = item.id === 1
          ? firstJob.promise
          : item.id === 2
            ? Promise.reject(new Error("translation failed"))
            : Promise.resolve("output.pdf");
        return result.finally(() => { active--; });
      },
    },
  });
  harness.queue.onJobFailed = (job, error) => failures.push([job.attachmentID, error.message]);
  for (const id of [1, 2, 3]) harness.items.set(id, { id });

  const initial = harness.queue.enqueue([1, 2, 1]);
  assert.deepEqual(
    { addedCount: initial.addedCount, duplicateCount: initial.duplicateCount, aheadCount: initial.aheadCount },
    { addedCount: 2, duplicateCount: 1, aheadCount: 0 }
  );
  assert.deepEqual(started, [1], "only the first FIFO job may start initially");

  const additional = harness.queue.enqueue([2, 3]);
  assert.deepEqual(
    { addedCount: additional.addedCount, duplicateCount: additional.duplicateCount, aheadCount: additional.aheadCount },
    { addedCount: 1, duplicateCount: 1, aheadCount: 2 }
  );
  assert.ok(harness.notices[1].descriptions.some(text => text.includes("已忽略 1 篇重复任务")));

  const idle = harness.queue.onIdle();
  firstJob.resolve("first.pdf");
  await idle;

  assert.deepEqual(started, [1, 2, 3], "jobs must remain FIFO and duplicates must not run");
  assert.deepEqual(failures, [[2, "translation failed"]]);
  assert.equal(maxActive, 1, "the queue must never run two jobs concurrently");
}

async function validatesSettingsBeforeStarting() {
  const failures = [];
  let runnerCalls = 0;
  const harness = makeHarness({
    settings: { baseURL: "", apiKey: "", model: "" },
    runner: { translate: async () => { runnerCalls++; return "output.pdf"; } },
  });
  harness.queue.onJobFailed = (job, error) => failures.push(error.message);
  harness.items.set(10, { id: 10 });

  harness.queue.enqueue([10]);
  await harness.queue.onIdle();

  assert.equal(runnerCalls, 0);
  assert.equal(failures.length, 1);
  assert.match(failures[0], /Zotero 设置/);
  assert.match(failures[0], /API Base URL/);
  assert.doesNotMatch(failures[0], /BabelDOC 路径/);
}

async function rechecksTranslatablePDFBeforeRunnerStarts() {
  const firstTranslation = deferred();
  const started = [];
  const harness = makeHarness({
    runner: {
      translate(item) {
        started.push(item.id);
        return item.id === 44 ? firstTranslation.promise : Promise.resolve("unexpected.pdf");
      },
    },
  });
  const firstItem = { id: 44 };
  const queuedItem = { id: 45, translatable: true };
  harness.items.set(firstItem.id, firstItem);
  harness.items.set(queuedItem.id, queuedItem);

  harness.queue.enqueue([firstItem.id, queuedItem.id]);
  assert.deepEqual(started, [44], "the second item waits behind the active translation");
  queuedItem.translatable = false;

  const idle = harness.queue.onIdle();
  firstTranslation.resolve("first.pdf");
  await idle;

  assert.deepEqual(started, [44], "a queued attachment that is no longer translatable must be skipped");
  assert.equal(harness.progressWindows.length, 1, "skipped items must not open a translation progress window");
  assert.deepEqual(harness.failures, [], "a stale queue entry is not a translation failure");
}

async function cancelAbortsRunningJobAndDropsPendingJobs() {
  const started = [];
  const failures = [];
  let observedSignal;
  let abortEvents = 0;
  const harness = makeHarness({
    useAbortController: false,
    runner: {
      translate(item, { signal }) {
        started.push(item.id);
        observedSignal = signal;
        return new Promise((resolve, reject) => {
          signal.addEventListener("abort", () => {
            abortEvents++;
            reject(new Error("aborted"));
          }, { once: true });
        });
      },
    },
  });
  harness.queue.onJobFailed = (_job, error) => failures.push(error.message);
  harness.items.set(20, { id: 20 });
  harness.items.set(21, { id: 21 });

  harness.queue.enqueue([20, 21]);
  const idle = harness.queue.onIdle();
  harness.queue.shutdown();
  await idle;

  assert.equal(observedSignal.aborted, true, "shutdown cancellation must reach the runner signal");
  assert.equal(abortEvents, 1, "the fallback signal must dispatch abort listeners");
  assert.deepEqual(started, [20], "pending jobs must be discarded on cancellation");
  assert.deepEqual(failures, [], "shutdown cancellation must not be reported as a translation failure");
  harness.flushAlerts();
  assert.deepEqual(harness.alerts, [], "shutdown cancellation must not show a failure alert");
}

async function failureAlertsAreDeferredRedactedAndQueueContinues() {
  const secret = "DEMO_SECRET_KEY_987";
  const started = [];
  const harness = makeHarness({
    settings: {
      baseURL: "https://example.test/v1",
      apiKey: secret,
      model: "model",
    },
    runner: {
      async translate(item) {
        started.push(item.id);
        if (item.id === 32) {
          throw new Error(
            "HTTP 401 Unauthorized\nAuthorization: Bearer bearer-secret-value\n" +
            "scope_Bearer boundary-bearer-secret prefix_sk-1234567890 " + secret
          );
        }
        return "next-output.pdf";
      },
    },
  });
  harness.items.set(32, { id: 32, getField: () => "First paper" });
  harness.items.set(33, { id: 33, getField: () => "Second paper" });

  harness.queue.enqueue([32, 33]);
  await harness.queue.onIdle();

  assert.deepEqual(started, [32, 33], "the next job must start without waiting for the alert");
  assert.deepEqual(harness.alerts, [], "the alert should be scheduled after the queue advances");
  harness.flushAlerts();

  assert.equal(harness.alerts.length, 1, "a failed job should produce exactly one alert");
  assert.equal(harness.alerts[0].parent, harness.mainWindow, "the prompt should use Zotero's latest main window");
  assert.equal(harness.alerts[0].title, "翻译失败");
  assert.match(harness.alerts[0].message, /First paper/);
  assert.match(harness.alerts[0].message, /API key 无效或无权限/);
  assert.match(harness.alerts[0].message, /技术详情：.*HTTP 401/);
  for (const sensitive of [secret, "bearer-secret-value", "boundary-bearer-secret", "sk-1234567890"]) {
    assert.ok(!harness.alerts[0].message.includes(sensitive), "alerts must redact API credentials");
    assert.ok(!harness.logs.join("\n").includes(sensitive), "logs must redact API credentials");
  }
}

async function failureAlertsMapCommonCausesAndShutdownSuppressesPendingAlerts() {
  const cases = [
    ["HTTP 403 Forbidden", "API key 无效或无权限"],
    ["invalid_api_key", "API key 无效或无权限"],
    ["HTTP 404 Client Error: Not Found", "接口地址或模型名不正确"],
    ["model_not_found", "接口地址或模型名不正确"],
    ["HTTP 429 Too Many Requests", "请求过于频繁或额度不足（接口可能已达到请求次数上限）"],
    ["insufficient_quota", "请求过于频繁或额度不足（接口可能已达到请求次数上限）"],
    ["BabelDOC 不支持包含查询参数的 API 地址", "BabelDOC 不支持包含查询参数的接口地址"],
    ["ConnectError: ECONNREFUSED", "无法连接到接口地址"],
    ["spawn C:\\babeldoc\\python.exe ENOENT", "找不到 BabelDOC，请重新翻译或在设置中点击“修复环境”"],
    ["python.exe not found", "找不到 BabelDOC，请重新翻译或在设置中点击“修复环境”"],
    ["找不到 BabelDOC 环境中的 python.exe", "找不到 BabelDOC，请重新翻译或在设置中点击“修复环境”"],
    ["No module named 'babeldoc'", "找不到 BabelDOC，请重新翻译或在设置中点击“修复环境”"],
    ["PDF 文件不可用：附件可能尚未下载", "PDF 文件不存在（可能尚未下载）"],
    ["BabelDOC 没有返回可导入的译本文件。", "BabelDOC 未生成可导入的中文 PDF"],
    ["Zotero 未能创建译本附件。", "无法导入中文译本"],
  ];
  const harness = makeHarness();

  for (let index = 0; index < cases.length; index++) {
    harness.queue.onJobFailed(
      { attachmentID: index + 60, attachmentTitle: "Failure case " + index },
      new Error(cases[index][0])
    );
  }
  harness.flushAlerts();

  assert.equal(harness.alerts.length, cases.length, "each failure should retain its own alert");
  for (let index = 0; index < cases.length; index++) {
    assert.match(harness.alerts[index].message, new RegExp(cases[index][1]));
    assert.match(harness.alerts[index].message, /技术详情：/);
  }

  const validationHarness = makeHarness();
  validationHarness.queue.onJobFailed(
    { attachmentID: 98, attachmentTitle: "Invalid request limit" },
    new Error("单篇最多请求次数必须是大于或等于 1 的整数。"),
  );
  validationHarness.flushAlerts();
  assert.match(validationHarness.alerts[0].message, /翻译设置无效/);
  assert.doesNotMatch(validationHarness.alerts[0].message, /请求过于频繁或额度不足/);

  const versionHarness = makeHarness();
  const versionError = new Error("version mismatch");
  versionError.kind = "babeldoc_version";
  versionHarness.queue.onJobFailed(
    { attachmentID: 100, attachmentTitle: "Unsupported BabelDOC version" },
    versionError,
  );
  versionHarness.flushAlerts();
  assert.match(
    versionHarness.alerts[0].message,
    /自动安装的 BabelDOC 环境版本不正确，请在设置中点击“修复环境”/,
  );

  const requestCapHarness = makeHarness();
  const requestCapError = new Error("upstream quota response");
  requestCapError.kind = "request_cap";
  requestCapHarness.queue.onJobFailed(
    { attachmentID: 99, attachmentTitle: "Local request cap" },
    requestCapError,
  );
  requestCapHarness.flushAlerts();
  assert.match(requestCapHarness.alerts[0].message, /已达到单篇请求次数上限/);
  assert.doesNotMatch(requestCapHarness.alerts[0].message, /请求过于频繁或额度不足/);

  const htmlHarness = makeHarness();
  const htmlError = [
    "HTTP 429: <!DOCTYPE html><html><head><title>Request Limit Exceeded</title></head>",
    '<body><div id="requestId">rq-1792116551</div><script>document.getElementById("requestId").textContent=t</script></body></html>',
    "2026-10-16 10:09:06,525 INFO babeldoc: Translation completed. Total: 484, Successful: 70, Fallback: 414",
  ].join("\n");
  htmlHarness.queue.onJobFailed(
    { attachmentID: 95, attachmentTitle: "HTML provider failure" },
    new Error(htmlError),
  );
  htmlHarness.flushAlerts();
  assert.match(htmlHarness.alerts[0].message, /请求过于频繁或额度不足（接口可能已达到请求次数上限）/);
  assert.match(htmlHarness.alerts[0].message, /HTTP 429：Request Limit Exceeded/);
  assert.match(htmlHarness.alerts[0].message, /requestId: rq-1792116551/);
  assert.match(htmlHarness.alerts[0].message, /484 段中 70 段成功（414 段保留原文）/);
  assert.ok(!htmlHarness.alerts[0].message.includes("<script"), "HTML diagnostics must not expose raw markup");
  assert.ok(!htmlHarness.alerts[0].message.includes("Translation completed."), "technical details must omit trailing BabelDOC INFO lines");

  const htmlBodyHarness = makeHarness();
  const bodyOnlyError = new Error("upstream request failed");
  bodyOnlyError.response = {
    status: 429,
    body: '<html><title>Request Limit Exceeded</title><div id="requestId">rq-body-429</div></html>',
  };
  htmlBodyHarness.queue.onJobFailed(
    { attachmentID: 97, attachmentTitle: "HTML response body" },
    bodyOnlyError,
  );
  htmlBodyHarness.flushAlerts();
  assert.match(htmlBodyHarness.alerts[0].message, /请求过于频繁或额度不足（接口可能已达到请求次数上限）/);
  assert.match(htmlBodyHarness.alerts[0].message, /HTTP 429：Request Limit Exceeded/);
  assert.match(htmlBodyHarness.alerts[0].message, /requestId: rq-body-429/);
  assert.ok(!htmlBodyHarness.alerts[0].message.includes("<html"));

  const genericHTMLHarness = makeHarness();
  genericHTMLHarness.queue.onJobFailed(
    { attachmentID: 96, attachmentTitle: "HTML response" },
    new Error("<!doctype html><html><head><title>Access Denied</title></head></html>"),
  );
  genericHTMLHarness.flushAlerts();
  assert.match(genericHTMLHarness.alerts[0].message, /接口返回了网页而不是 API 响应/);
  assert.ok(!genericHTMLHarness.alerts[0].message.includes("<html"));

  const shutdownHarness = makeHarness();
  shutdownHarness.queue.onJobFailed(
    { attachmentID: 70, attachmentTitle: "Pending alert" },
    new Error("process spawn failed")
  );
  shutdownHarness.queue.shutdown();
  shutdownHarness.flushAlerts();
  assert.deepEqual(shutdownHarness.alerts, [], "shutdown must suppress an alert that has not been shown");
}

async function failureLogsOmitRunnerAndHandlerErrors() {
  const secret = "DEMO_SECRET_KEY_987";
  const defaultHook = makeHarness({
    settings: {
      baseURL: "https://example.test/v1",
      apiKey: secret,
      model: "model",
    },
    runner: {
      translate: async () => { throw new Error("request failed with key " + secret); },
    },
  });
  defaultHook.items.set(30, { id: 30 });
  defaultHook.queue.enqueue([30]);
  await defaultHook.queue.onIdle();
  assert.ok(defaultHook.logs.some(line => line.includes("attachment 30")));
  assert.ok(!defaultHook.logs.join("\n").includes(secret), "default failure logging must omit runner details");

  const throwingHook = makeHarness({
    settings: {
      baseURL: "https://example.test/v1",
      apiKey: secret,
      model: "model",
    },
    runner: {
      translate: async () => { throw new Error("runner error " + secret); },
    },
  });
  throwingHook.queue.onJobFailed = () => { throw new Error("handler error " + secret); };
  throwingHook.items.set(31, { id: 31 });
  throwingHook.queue.enqueue([31]);
  await throwingHook.queue.onIdle();
  assert.ok(throwingHook.logs.some(line => line.includes("onJobFailed failed for attachment 31")));
  assert.ok(!throwingHook.logs.join("\n").includes(secret), "failure-handler logging must omit error details");
}

async function cleanupFollowsImportAndProgressShowsQueuePosition() {
  const events = [];
  const harness = makeHarness({
    runner: {
      async translate(item, { onProgress }) {
        if (item.id === 40) {
          onProgress({ overall: 42.5, stage: "翻译段落", requests: 2, planned: 2 });
          return {
            outputPath: "first-translation.pdf",
            requestCount: 2,
            cleanup: async () => { events.push("cleanup"); },
          };
        }
        return "second-translation.pdf";
      },
    },
    importer: {
      async importResult(item, outputPath) {
        events.push("import:" + item.id + ":" + outputPath);
      },
    },
  });
  harness.items.set(40, {
    id: 40,
    getField: () => "First paper",
    getItemTypeIconName: () => "attachment-pdf",
  });
  harness.items.set(41, { id: 41 });

  harness.queue.enqueue([40, 41]);
  await harness.queue.onIdle();

  assert.deepEqual(events, [
    "import:40:first-translation.pdf",
    "cleanup",
    "import:41:second-translation.pdf",
  ], "the work directory must remain until import finishes");
  assert.equal(harness.progressWindows.length, 2, "each job should have one progress window");
  assert.equal(
    harness.progressWindows[0].itemProgressArgs[0],
    "attachment-pdf",
    "the ItemProgress icon argument should be the attachment item type",
  );
  assert.deepEqual(
    harness.progressWindows[0].closeTimerArgs,
    [5000],
    "the completion window close timer must not require mouse hover",
  );
  assert.ok(
    harness.progressWindows[0].texts.some(text => text.includes("43% · 请求 2/2（队列中还有 1 篇）")),
    "BabelDOC progress should show the rounded percentage and queued count",
  );
  assert.ok(
    harness.progressWindows[0].texts.some(text => text.includes("请求 2/2")),
    "translation progress should show actual and planned API request counts",
  );
  assert.ok(
    harness.progressWindows[0].texts.some(text => text.startsWith("已完成：First paper（中文译本）")),
    "the completion progress should name the imported translated attachment",
  );
  assert.ok(
    harness.progressWindows[0].texts.includes("已完成：First paper（中文译本）（共请求 2 次）"),
    "the completion progress should show the total request count",
  );
}

async function partialFailureWarningShowsInCompletionProgress() {
  const harness = makeHarness({
    runner: {
      async translate() {
        return {
          outputPath: "partial-translation.pdf",
          warning: { failed_paragraphs: 2, message: "Connection error." },
        };
      },
    },
  });
  harness.items.set(42, { id: 42 });
  harness.queue.enqueue([42]);
  await harness.queue.onIdle();

  assert.ok(
    harness.progressWindows[0].texts.some(text =>
      text.includes("已完成：PDF（中文译本）（有 2 段翻译失败，保留原文）")
    ),
    "partial translation failures should be shown when the job completes",
  );
}

async function uncertainTableWarningShowsInCompletionProgress() {
  const harness = makeHarness({
    runner: {
      async translate() {
        return {
          outputPath: "tables.pdf",
          warning: {
            failed_paragraphs: 0,
            message: "表 5（物理第 2 页）无法可靠识别表头，已整表保留原文。",
            tables: [{ table: "表 5", page: 2, reason: "" }],
          },
        };
      },
    },
  });
  harness.items.set(44, { id: 44 });
  harness.queue.enqueue([44]);
  await harness.queue.onIdle();

  assert.ok(
    harness.progressWindows[0].texts.some(text =>
      text.includes("（表 5（第 2 页）未能可靠识别表头，已整表保留原文）")
    ),
    "uncertain tables should name the table and physical page when the job completes",
  );
}

async function cleanupRunsWhenImportFails() {
  const events = [];
  const failures = [];
  const harness = makeHarness({
    runner: {
      async translate() {
        return {
          outputPath: "failed-import.pdf",
          cleanup: async () => { events.push("cleanup"); },
        };
      },
    },
    importer: {
      async importResult() {
        events.push("import");
        throw new Error("import failed");
      },
    },
  });
  harness.queue.onJobFailed = (job, error) => failures.push([job.attachmentID, error.message]);
  harness.items.set(43, { id: 43 });

  harness.queue.enqueue([43]);
  await harness.queue.onIdle();

  assert.deepEqual(events, ["import", "cleanup"]);
  assert.deepEqual(failures, [[43, "import failed"]]);
}

async function maintenanceLockRefusesActiveWorkAndPausesNewJobs() {
  const started = [];
  const firstStarted = deferred();
  const finishFirst = deferred();
  const harness = makeHarness({
    runner: {
      async translate(item) {
        started.push(item.id);
        if (item.id === 1) {
          firstStarted.resolve();
          await finishFirst.promise;
        }
        return "translation-" + item.id + ".pdf";
      },
    },
  });
  harness.items.set(1, { id: 1 });
  harness.items.set(2, { id: 2 });

  harness.queue.enqueue([1]);
  await firstStarted.promise;
  const busyLock = harness.queue.acquireMaintenanceLock();
  assert.equal(busyLock.acquired, false, "maintenance must be refused while a translation is running");
  assert.equal(busyLock.reason, "translation");
  finishFirst.resolve();
  await harness.queue.onIdle();

  const lock = harness.queue.acquireMaintenanceLock();
  assert.equal(lock.acquired, true);
  harness.queue.enqueue([2]);
  await Promise.resolve();
  assert.deepEqual(started, [1], "queued work must not start while environment maintenance holds the lock");
  const idle = harness.queue.onIdle();
  lock.release();
  await idle;
  assert.deepEqual(started, [1, 2], "queued work should resume when maintenance releases the lock");
}

async function outputModeIsPassedPerJobAndDefaultsToMono() {
  const calls = [];
  const harness = makeHarness({
    runner: {
      async translate(item, options) {
        calls.push({ id: item.id, outputMode: options.outputMode });
        return "output.pdf";
      },
    },
  });
  for (const id of [1, 2, 3]) harness.items.set(id, { id });

  const first = harness.queue.enqueue([1], "dual");
  assert.equal(first.addedCount, 1);
  const duplicate = harness.queue.enqueue([1], "mono");
  assert.equal(duplicate.addedCount, 0, "the same PDF cannot be queued twice with another output mode");
  assert.equal(duplicate.duplicateCount, 1);
  harness.queue.enqueue([2]);
  harness.queue.enqueue([3], "both");

  await harness.queue.onIdle();
  assert.deepEqual(calls, [
    { id: 1, outputMode: "dual" },
    { id: 2, outputMode: "mono" },
    { id: 3, outputMode: "mono" },
  ], "each job keeps its output mode; missing or unknown modes fall back to mono");
}

async function importsBilingualArtifactsInOrder() {
  const harness = makeHarness({ runner: { translate: async () => ({
    outputPath: "mono.pdf",
    outputPaths: [
      { kind: "mono", path: "mono.pdf" },
      { kind: "dual", path: "dual.pdf" },
    ],
    cleanup() {},
  }) } });
  harness.items.set(1, { id: 1 });
  harness.queue.enqueue([1]);
  await harness.queue.onIdle();
  assert.equal(harness.imports.length, 2, "both selected artifacts must be imported");
  assert.equal(
    harness.imports.map(entry => entry[1]).join(","),
    "mono.pdf,dual.pdf",
    "artifacts keep the requested order",
  );
}

(async () => {
  await nativePopupUnloadStopsTimersAndInvalidatesLateLocalization();
  await outputModeIsPassedPerJobAndDefaultsToMono();
  await importsBilingualArtifactsInOrder();
  await failedShowRestoresRegistrationAndRemovesUnloadListener();
  await silentStagesKeepShowingRealElapsedTime();
  await stageTimersStayWithTheirJobAndStopOnTerminalPaths();
  await elapsedLocalizationCannotOverwriteNewerOrFinishedProgress();
  await localizationErrorsFallBackWithoutStoppingTheQueue();
  await timersStopBeforeAsyncResultCleanupCompletes();
  await fifoDedupeAndContinueAfterFailure();
  await validatesSettingsBeforeStarting();
  await rechecksTranslatablePDFBeforeRunnerStarts();
  await cancelAbortsRunningJobAndDropsPendingJobs();
  await failureAlertsAreDeferredRedactedAndQueueContinues();
  await failureAlertsMapCommonCausesAndShutdownSuppressesPendingAlerts();
  await failureLogsOmitRunnerAndHandlerErrors();
  await cleanupFollowsImportAndProgressShowsQueuePosition();
  await partialFailureWarningShowsInCompletionProgress();
  await uncertainTableWarningShowsInCompletionProgress();
  await cleanupRunsWhenImportFails();
  await maintenanceLockRefusesActiveWorkAndPausesNewJobs();
  process.stdout.write("translation queue regression cases passed\n");
})().catch(error => {
  process.stderr.write(error.stack + "\n");
  process.exitCode = 1;
});
