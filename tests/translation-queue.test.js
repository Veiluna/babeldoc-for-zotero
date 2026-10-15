const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../addon/content/modules/queue.js"),
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
  const failures = [];
  const logs = [];
  const items = new Map();
  const settings = options.settings || {
    baseURL: "https://example.test/v1",
    apiKey: "key",
    model: "model",
    babeldocPath: "C:\\babeldoc\\python.exe",
  };
  const context = {
    ZoteroTranslateFiles: { log: message => logs.push(String(message)) },
    ZoteroTranslateFilesConfig: { getSettings: () => settings },
    Zotero: {
      Items: { get: id => items.get(id) },
      ProgressWindow: function () {
        const notice = { descriptions: [], shown: false };
        notices.push(notice);
        this.changeHeadline = () => {};
        this.addDescription = value => notice.descriptions.push(value);
        this.show = () => { notice.shown = true; };
        this.startCloseTimer = () => {};
      },
      debug: message => logs.push(String(message)),
    },
    ZoteroTranslateFilesRunner: options.runner || { translate: async () => "output.pdf" },
  };
  if (options.useAbortController !== false) context.AbortController = AbortController;
  vm.runInNewContext(source, context, { filename: "queue.js" });

  const queue = context.ZoteroTranslateFilesQueue;
  const originalFailureHandler = queue.onJobFailed;
  queue.onJobFailed = (job, error) => {
    failures.push({ id: job.attachmentID, message: String(error.message || error) });
    originalFailureHandler(job, error);
  };
  queue.init();

  return { queue, items, failures, logs, notices, settings };
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
    settings: { baseURL: "", apiKey: "", model: "", babeldocPath: "" },
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
  assert.match(failures[0], /BabelDOC 路径/);
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
}

async function failureLogsOmitRunnerAndHandlerErrors() {
  const secret = "DEMO_SECRET_KEY_987";
  const defaultHook = makeHarness({
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

(async () => {
  await fifoDedupeAndContinueAfterFailure();
  await validatesSettingsBeforeStarting();
  await cancelAbortsRunningJobAndDropsPendingJobs();
  await failureLogsOmitRunnerAndHandlerErrors();
  process.stdout.write("translation queue regression cases passed\n");
})().catch(error => {
  process.stderr.write(error.stack + "\n");
  process.exitCode = 1;
});
