// main.js 对阅读器划词钩子（#44）的接线：启动时注册 renderToolbar、补挂已打开阅读器，
// 关闭时注销监听并调用模块 shutdown；模块缺失或接口异常时记录结构化诊断、不影响插件。
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const repositoryRoot = path.join(__dirname, "..");
const mainSource = fs.readFileSync(path.join(repositoryRoot, "addon/content/main.js"), "utf8");
const bootstrapSource = fs.readFileSync(path.join(repositoryRoot, "addon/bootstrap.js"), "utf8");

function makeContext({ selection, reader, lines } = {}) {
  const module = () => ({ init() {} });
  return {
    Zotero: { debug: (line) => lines && lines.push(line), Reader: reader },
    ZoteroTranslateFilesConfig: { migrateLegacyPreferences() {} },
    ZoteroTranslateFilesPreferences: module(),
    ZoteroTranslateFilesMenu: module(),
    ZoteroTranslateFilesQueue: module(),
    ZoteroTranslateFilesRunner: module(),
    ZoteroTranslateFilesImporter: module(),
    ZoteroTranslateFilesReaderSelection: selection,
  };
}

function loadPlugin(context) {
  vm.runInNewContext(mainSource + "\nthis.plugin = ZoteroTranslateFiles;", context, {
    filename: "main.js",
  });
  return context.plugin;
}

function makeReaderApi(openReaders) {
  return {
    _readers: openReaders,
    listeners: [],
    registerEventListener(type, handler, pluginID) {
      this.listeners.push({ type, handler, pluginID });
    },
    unregisterEventListener(type, handler) {
      this.listeners = this.listeners.filter((x) => !(x.type === type && x.handler === handler));
    },
  };
}

function makeSelection() {
  const calls = { init: [], attach: [], shutdown: 0 };
  return {
    calls,
    init(options) { calls.init.push(options); },
    attachToReader(reader) { calls.attach.push(reader); return Promise.resolve(true); },
    shutdown() { calls.shutdown++; },
  };
}

const PREFIX = "BabelDOC for Zotero: ";

// 每行都是统一调试前缀后接一行可解析的 JSON。
function parseLine(line) {
  assert.ok(line.startsWith(PREFIX), "使用统一调试前缀");
  return JSON.parse(line.slice(PREFIX.length));
}

// 取出接线写出的结构化诊断。
function wiringEvents(lines) {
  return lines.filter((line) => line.includes("reader_selection_wiring")).map(parseLine);
}

async function registersAndAttachesExistingReaders() {
  const existing = { id: "open-reader" };
  const readerApi = makeReaderApi([existing]);
  const selection = makeSelection();
  const lines = [];
  const plugin = loadPlugin(makeContext({ selection, reader: readerApi, lines }));
  plugin.init({ id: "babeldoc-for-zotero@veiluna.github.io", version: "0.7.0", rootURI: "" });

  assert.equal(selection.calls.init.length, 1, "模块启动一次");
  assert.deepEqual(selection.calls.attach, [existing], "已打开的阅读器补挂");
  assert.equal(readerApi.listeners.length, 1);
  assert.equal(readerApi.listeners[0].type, "renderToolbar");
  assert.equal(readerApi.listeners[0].pluginID, "babeldoc-for-zotero@veiluna.github.io");

  // 模块传来的诊断行原样输出，仍可解析。
  selection.calls.init[0].log('{"event":"reader_selection","reason":"x"}');
  assert.deepEqual(parseLine(lines.at(-1)), { event: "reader_selection", reason: "x" });

  const opened = { id: "new-reader" };
  readerApi.listeners[0].handler({ reader: opened });
  assert.deepEqual(selection.calls.attach, [existing, opened], "新阅读器经 renderToolbar 挂钩");

  plugin.shutdown();
  assert.equal(readerApi.listeners.length, 0, "关闭时注销监听");
  assert.equal(selection.calls.shutdown, 1, "关闭时调用模块 shutdown");
  assert.deepEqual(wiringEvents(lines), [], "正常路径不写故障诊断");
}

async function attachRejectionIsLoggedNotThrown() {
  const lines = [];
  const selection = makeSelection();
  selection.attachToReader = () => Promise.reject(new TypeError("boom secret"));
  const plugin = loadPlugin(makeContext({ selection, reader: makeReaderApi([{}]), lines }));
  plugin.init({ id: "id", version: "0.7.0", rootURI: "" });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(wiringEvents(lines), [
    { event: "reader_selection_wiring", reason: "attach_failed", count: 1, error: "TypeError" },
  ]);
  assert.ok(!lines.some((line) => line.includes("secret")), "日志不含异常消息正文");
}

async function skippedPathsAreDiagnosed() {
  let lines = [];
  const withoutModule = loadPlugin(makeContext({ selection: undefined, reader: makeReaderApi([]), lines }));
  withoutModule.init({ id: "id", version: "0.7.0", rootURI: "" });
  assert.deepEqual(wiringEvents(lines), [
    { event: "reader_selection_wiring", reason: "module_unavailable", count: 1 },
  ]);
  withoutModule.shutdown();

  lines = [];
  const selection = makeSelection();
  const withoutReader = loadPlugin(makeContext({ selection, reader: undefined, lines }));
  withoutReader.init({ id: "id", version: "0.7.0", rootURI: "" });
  assert.equal(selection.calls.init.length, 0, "没有 Zotero.Reader 时不启动");
  assert.deepEqual(wiringEvents(lines), [
    { event: "reader_selection_wiring", reason: "reader_api_unavailable", count: 1 },
  ]);
  withoutReader.shutdown();
  assert.equal(selection.calls.shutdown, 1, "shutdown 仍可安全调用");

  lines = [];
  const noList = makeReaderApi(undefined);
  const selectionNoList = makeSelection();
  const withoutList = loadPlugin(makeContext({ selection: selectionNoList, reader: noList, lines }));
  withoutList.init({ id: "id", version: "0.7.0", rootURI: "" });
  assert.equal(noList.listeners.length, 1, "仍注册新阅读器监听");
  assert.deepEqual(wiringEvents(lines), [
    { event: "reader_selection_wiring", reason: "open_readers_unavailable", count: 1 },
  ]);

  lines = [];
  const throwingApi = makeReaderApi([]);
  throwingApi.registerEventListener = () => { throw new RangeError("nope secret"); };
  throwingApi.unregisterEventListener = () => { throw new Error("x"); };
  const failing = makeSelection();
  failing.shutdown = () => { throw new TypeError("y"); };
  const plugin = loadPlugin(makeContext({ selection: failing, reader: throwingApi, lines }));
  plugin.init({ id: "id", version: "0.7.0", rootURI: "" });
  plugin.readerToolbarHandler = () => {};
  plugin.shutdown();
  assert.deepEqual(wiringEvents(lines), [
    { event: "reader_selection_wiring", reason: "start_failed", count: 1, error: "RangeError" },
    { event: "reader_selection_wiring", reason: "unregister_failed", count: 1, error: "Error" },
    { event: "reader_selection_wiring", reason: "shutdown_failed", count: 1, error: "TypeError" },
  ]);
  assert.ok(!lines.some((line) => line.includes("secret")));
}

async function bootstrapLoadsModuleBeforeMain() {
  // 执行 bootstrap.startup，记录实际加载顺序。
  const loaded = [];
  const context = {
    Zotero: {
      initializationPromise: Promise.resolve(),
      debug() {},
      getMainWindows: () => [],
    },
    Services: {
      scriptloader: {
        loadSubScript(uri) {
          loaded.push(uri.replace(/^.*content\//, "content/"));
          if (uri.endsWith("content/modules/installer.js")) {
            context.ZoteroTranslateFilesInstaller = { activate() {} };
          }
          if (uri.endsWith("content/main.js")) {
            context.ZoteroTranslateFiles = { init() {}, shutdown() {} };
            throw new Error("stop after main.js");
          }
        },
      },
    },
  };
  vm.runInNewContext(bootstrapSource + "\nthis.startup = startup;", context, {
    filename: "bootstrap.js",
  });
  await context.startup({ id: "id", version: "0.7.0", rootURI: "jar:addon/" });
  const selectionIndex = loaded.indexOf("content/modules/readerSelection.js");
  const mainIndex = loaded.indexOf("content/main.js");
  assert.ok(selectionIndex >= 0, "bootstrap 加载 readerSelection.js");
  assert.ok(selectionIndex < mainIndex, "在 main.js 之前加载");
}

(async () => {
  await registersAndAttachesExistingReaders();
  await attachRejectionIsLoggedNotThrown();
  await skippedPathsAreDiagnosed();
  await bootstrapLoadsModuleBeforeMain();
  process.stdout.write("reader selection wiring regression cases passed\n");
})().catch((error) => {
  process.stderr.write((error && error.stack) || String(error));
  process.exit(1);
});
