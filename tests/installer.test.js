const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const expectedWheelHash = "4f693cd4feb52f08f30474c4612f24be2050472609be65b9960b635f286da284";

const installerSource = fs.readFileSync(
  path.join(__dirname, "../addon/content/modules/installer.js"),
  "utf8",
);
const configSource = fs.readFileSync(
  path.join(__dirname, "../addon/content/modules/config.js"),
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

function within(promise, message) {
  let timeout;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error(message)), 1000);
    }),
  ]).finally(() => clearTimeout(timeout));
}

function stream(text) {
  let pending = Buffer.from(text || "", "utf8");
  return {
    async read() {
      const value = new Uint8Array(pending);
      pending = Buffer.alloc(0);
      return value;
    },
  };
}

function makeHarness(options = {}) {
  const files = new Map();
  const textFiles = new Map();
  const calls = [];
  const downloads = [];
  const prompts = [];
  const localizationCalls = [];
  const writes = [];
  const removeCalls = [];
  const events = [];
  const root = options.localAppData === undefined
    ? "C:\\Users\\Test\\AppData\\Local"
    : options.localAppData;
  const dirs = ["C:\\Zotero"];
  if (root) dirs.push(root);
  const base = root ? root + "\\babeldoc-for-zotero" : "";
  const join = (...parts) => parts.filter(Boolean).join("\\").replace(/\\{2,}/g, "\\");
  const warmupStarted = deferred();
  const warmupWait = deferred();
  const readinessCheckStarted = deferred();
  const readinessWait = deferred();
  let pythonInstalled = !!options.existingEnvironment;
  let environmentCreated = !!options.existingEnvironment;
  let packageInstalled = !!options.existingEnvironment || !!options.wrongPackageVersion;
  let wrongPackageVersion = !!options.wrongPackageVersion;
  let failedPip = false;
  let killCount = 0;
  let downloadCancelCount = 0;
  let warmupWasBlocked = false;
  let promptChoice = 1;
  let maintenanceLocked = false;
  let readinessReadCount = 0;
  const localizedRepairStrings = options.locale === "en-US" ? {
    "babeldoc-for-zotero-pref-environment-repair-confirm-title": "Repair translation environment",
    "babeldoc-for-zotero-pref-environment-repair-confirm-message": "This will delete and reinstall the translation environment (about 2 minutes). Continue?",
    "babeldoc-for-zotero-pref-environment-repair-confirm": "Reinstall",
    "babeldoc-for-zotero-pref-environment-repair-cancel": "Cancel",
  } : {
    "babeldoc-for-zotero-pref-environment-repair-confirm-title": "修复翻译环境",
    "babeldoc-for-zotero-pref-environment-repair-confirm-message": "将删除并重新安装翻译环境（约 2 分钟），确定吗？",
    "babeldoc-for-zotero-pref-environment-repair-confirm": "重新安装",
    "babeldoc-for-zotero-pref-environment-repair-cancel": "取消",
  };
  const mainWindow = {
    main: true,
    document: {
      l10n: {
        async formatValue(messageID) {
          localizationCalls.push(messageID);
          return localizedRepairStrings[messageID];
        },
      },
    },
    MozXULElement: {
      insertFTLIfNeeded(name) { assert.equal(name, "babeldoc-for-zotero.ftl"); },
    },
  };

  if (options.existingEnvironment) {
    files.set(base + "\\uv\\uv.exe", true);
    files.set(base + "\\env\\Scripts\\python.exe", true);
  }

  const expectedDigest = Uint8Array.from(Buffer.from(expectedWheelHash, "hex"));
  const cryptoStub = {
    subtle: {
      async digest(_algorithm, bytes) {
        if (options.hashMismatch) return new Uint8Array(32).buffer;
        assert.ok(bytes instanceof Uint8Array || ArrayBuffer.isView(bytes));
        return expectedDigest.slice().buffer;
      },
    },
  };

  const fileAPI = {
    pathToFile(filePath) {
      return {
        path: filePath,
        exists: () => files.has(filePath),
        isFile: () => files.has(filePath),
      };
    },
    async createDirectoryIfMissingAsync(directory) {
      if (dirs.includes(directory)) return;
      const parent = directory.slice(0, directory.lastIndexOf("\\"));
      if (!dirs.includes(parent)) throw new Error("Parent directory does not exist: " + parent);
      dirs.push(directory);
    },
  };

  const IOUtils = {
    async readUTF8(filePath) {
      if (filePath === base + "\\install.json") {
        readinessReadCount++;
        if (options.blockReadiness || options.blockReadinessAt === readinessReadCount) {
          readinessCheckStarted.resolve();
          await readinessWait.promise;
        }
      }
      if (!textFiles.has(filePath)) throw new Error("File not found: " + filePath);
      return textFiles.get(filePath);
    },
    async writeUTF8(filePath, text) {
      const directory = filePath.slice(0, filePath.lastIndexOf("\\"));
      assert.ok(dirs.includes(directory), "the log parent directory must exist before writing " + filePath);
      textFiles.set(filePath, String(text));
      writes.push([filePath, String(text)]);
    },
    async read(filePath) {
      if (!files.has(filePath)) throw new Error("File not found: " + filePath);
      return new Uint8Array([1, 2, 3]);
    },
    async remove(filePath, removeOptions = {}) {
      removeCalls.push({ path: filePath, options: Object.assign({}, removeOptions) });
      if (filePath === base) events.push("base-directory-removed");
      if (filePath === base && options.failBaseRemoval) throw new Error("file in use");
      files.delete(filePath);
      textFiles.delete(filePath);
      if (removeOptions.recursive) {
        for (const key of [...files.keys()]) if (key.startsWith(filePath + "\\")) files.delete(key);
        for (const key of [...textFiles.keys()]) if (key.startsWith(filePath + "\\")) textFiles.delete(key);
        for (let index = dirs.length - 1; index >= 0; index--) {
          if (dirs[index] === filePath || dirs[index].startsWith(filePath + "\\")) dirs.splice(index, 1);
        }
        if (filePath === base + "\\env") {
          environmentCreated = false;
          packageInstalled = false;
          wrongPackageVersion = false;
        }
        if (filePath === base) {
          pythonInstalled = false;
          environmentCreated = false;
          packageInstalled = false;
          wrongPackageVersion = false;
        }
      }
    },
  };

  const subprocess = {
    async call(callOptions) {
      calls.push(callOptions);
      const command = callOptions.command;
      const args = callOptions.arguments;
      const joinedArgs = args.join(" ");
      let exitCode = 0;
      let stdout = "";
      let stderr = "";
      if (args[0] === "--version") {
        stdout = options.uvVersionOutput || "uv 0.13.1 (abc123 2026-10-17 x86_64-pc-windows-msvc)\n";
      } else if (args[0] === "python" && args[1] === "find") {
        exitCode = pythonInstalled ? 0 : 1;
        if (pythonInstalled) stdout = base + "\\python\\cpython-3.12\\python.exe\n";
      } else if (args[0] === "python" && args[1] === "install") {
        pythonInstalled = true;
        stdout = "Installed Python 3.12\n";
      } else if (args[0] === "venv") {
        environmentCreated = true;
        files.set(base + "\\env\\Scripts\\python.exe", true);
        files.set(base + "\\env\\Scripts\\babeldoc.exe", true);
        stdout = "Creating virtual environment\n";
      } else if (args[0] === "-c" && args[1].includes("babeldoc=")) {
        stdout = [
          "python=" + (options.checkPythonVersion || "3.12"),
          "babeldoc=" + (options.checkBabeldocVersion || "0.6.4"),
          "bitstring=" + (options.checkBitstringVersion || "4.3.1"),
          "fonts=" + (options.checkFonts == null ? 3 : options.checkFonts),
          "models=" + (options.checkModels == null ? 2 : options.checkModels),
        ].join("\n") + "\n";
      } else if (args[0] === "-c" && args[1].includes("environment check")) {
        exitCode = options.failSmokeCheck ? 1 : 0;
        stdout = exitCode ? "" : "smoke=ok\n";
      } else if (args[0] === "-c" && args[1].includes("sys.version_info")) {
        exitCode = environmentCreated ? 0 : 1;
      } else if (args[0] === "-c" && args[1] === "import babeldoc.main") {
        exitCode = packageInstalled ? 0 : 1;
        if (exitCode) stderr = "ModuleNotFoundError: No module named 'babeldoc'\n";
      } else if (args[0] === "-c" && args[1].includes("import babeldoc.main")) {
        exitCode = packageInstalled && !wrongPackageVersion ? 0 : 1;
        if (exitCode) stderr = "AssertionError: unexpected package version\n";
      } else if (args[0] === "pip") {
        if (options.failPipOnce && !failedPip) {
          failedPip = true;
          exitCode = 1;
          stderr = "ERROR: temporary package index failure\n";
        } else {
          packageInstalled = true;
          wrongPackageVersion = false;
          if (args.includes("--reinstall-package") && args.includes("BabelDOC")) {
            files.set(base + "\\env\\Scripts\\babeldoc.exe", true);
          }
          stdout = "Successfully installed BabelDOC-0.6.4 " + (options.outputSecret || "") + "\n";
        }
      } else if (args[0] === "--warmup") {
        warmupStarted.resolve();
        if (options.blockWarmup && !warmupWasBlocked) {
          warmupWasBlocked = true;
          return {
            stdout: stream(stdout),
            stderr: stream(stderr),
            kill() {
              killCount++;
              events.push("blocked-install-child-killed");
              warmupWait.resolve({ exitCode: -9 });
            },
            wait() {
              return warmupWait.promise.then(result => {
                events.push("blocked-install-child-settled");
                return result;
              });
            },
          };
        }
        stdout = "Warmup complete\n";
      }

      return {
        stdin: { close() {} },
        stdout: stream(stdout),
        stderr: stream(stderr),
        kill() { killCount++; },
        async wait() { return { exitCode }; },
        get argsText() { return joinedArgs; },
      };
    },
  };

  const Zotero = {
    Prefs: {
      get(name) {
        if (name === "extensions.babeldoc-for-zotero.apiKey") return options.apiKey || "";
        return null;
      },
    },
    File: fileAPI,
    DataDirectory: { dir: "C:\\Zotero" },
    getMainWindow() { return mainWindow; },
    __babeldocForZoteroQueue: options.queue || {
      acquireMaintenanceLock() {
        if (options.queueBusy) return { acquired: false, reason: "translation" };
        if (maintenanceLocked) return { acquired: false, reason: "maintenance" };
        maintenanceLocked = true;
        return {
          acquired: true,
          release() { maintenanceLocked = false; },
        };
      },
    },
    HTTP: {
      async download(url, destination, downloadOptions) {
        downloads.push({ url, destination, downloadOptions });
        if (options.stallDownload && downloads.length === 1) {
          return new Promise((resolve, reject) => {
            options.downloadStarted.resolve(() => downloadOptions.cancellerReceiver(() => {
              downloadCancelCount++;
              reject(new Error("download aborted"));
            }));
          });
        }
        downloadOptions.onProgress(3, 3);
        files.set(destination, true);
      },
    },
  };
  if (options.blockReadiness) files.set(base + "\\env\\Scripts\\python.exe", true);
  if (options.previousInstaller) Zotero.__babeldocForZoteroInstaller = options.previousInstaller;

  const context = {
    Zotero,
    IOUtils,
    PathUtils: { join, tempDir: "C:\\Temp" },
    TextDecoder,
    crypto: cryptoStub,
    Services: {
      env: { get(name) { assert.equal(name, "LOCALAPPDATA"); return root; } },
      sysinfo: { getProperty(name) { assert.equal(name, "arch"); return options.architecture || "x86_64"; } },
      prompt: {
        BUTTON_TITLE_IS_STRING: 127,
        BUTTON_POS_0: 1,
        BUTTON_POS_1: 256,
        BUTTON_POS_1_DEFAULT: 0,
        confirmEx(parent, title, message, flags, retry, cancel) {
          prompts.push({ parent, title, message, flags, retry, cancel });
          if (options.promptChoices && options.promptChoices.length) return options.promptChoices.shift();
          return promptChoice;
        },
      },
    },
    Cc: {
      "@mozilla.org/libjar/zip-reader;1": {
        createInstance() {
          return {
            open(file) { assert.ok(file.path.endsWith("uv-0.13.1.whl")); },
            hasEntry(name) { return name === "uv-0.13.1.data/scripts/uv.exe"; },
            extract(_entry, destination) { files.set(destination.path, true); },
            close() {},
          };
        },
      },
    },
    Ci: { nsIZipReader: {} },
    ChromeUtils: {
      importESModule(uri) {
        assert.equal(uri, "resource://gre/modules/Subprocess.sys.mjs");
        return { Subprocess: subprocess };
      },
    },
  };
  vm.runInNewContext(configSource, context, { filename: "config.js" });
  vm.runInNewContext(installerSource, context, { filename: "installer.js" });

  return {
    installer: context.ZoteroTranslateFilesInstaller,
    files,
    textFiles,
    calls,
    downloads,
    prompts,
    localizationCalls,
    writes,
    removeCalls,
    events,
    dirs,
    base,
    warmupStarted,
    readinessCheckStarted,
    completeReadiness() { readinessWait.resolve(); },
    completeWarmup(exitCode = 0) { warmupWait.resolve({ exitCode }); },
    set promptChoice(value) { promptChoice = value; },
    get maintenanceLocked() { return maintenanceLocked; },
    get killCount() { return killCount; },
    get downloadCancelCount() { return downloadCancelCount; },
    setDownloadStarted(value) { options.downloadStarted = value; },
  };
}

async function pureConfigurationAndReadiness() {
  const harness = makeHarness();
  const test = harness.installer._test;
  assert.equal(harness.installer.__moduleVersion, 3);
  const installAbortController = harness.installer.createInstallAbortController();
  assert.equal(installAbortController.signal.aborted, false);
  installAbortController.abort();
  assert.equal(installAbortController.signal.aborted, true);
  assert.equal(test.isExpectedUVVersion("uv 0.13.1 (abc123 2026-10-17 x86_64-pc-windows-msvc)\n"), true);
  assert.equal(test.isExpectedUVVersion("uv 0.13.0 (abc123 2026-10-17 x86_64-pc-windows-msvc)\n"), false);
  assert.equal(test.isExpectedUVVersion("uv 0.13.10 (abc123 2026-10-17 x86_64-pc-windows-msvc)\n"), false);
  assert.equal(harness.installer.baseDir(), harness.base);
  assert.equal(await harness.installer.isReady(), false);
  const paths = test.pathsFor(harness.base);
  harness.files.set(paths.python, true);
  harness.textFiles.set(paths.installJSON, JSON.stringify({
    uv: "0.13.1", python: "3.12", babeldoc: "0.6.4", source: "china", completedAt: "2026-01-01T00:00:00.000Z",
  }));
  assert.equal(await harness.installer.isReady(), true);
  harness.textFiles.set(paths.installJSON, JSON.stringify({ uv: "0.13.0", python: "3.12", babeldoc: "0.6.4" }));
  assert.equal(await harness.installer.isReady(), false);

  assert.equal(test.selectWheel("x86_64").sha256, expectedWheelHash);
  assert.equal(test.selectWheel("aarch64").path.endsWith("win_arm64.whl"), true);
  assert.throws(() => test.selectWheel("arm"), /不支持的处理器架构/);
  assert.equal(test.normalizeSource("unexpected"), "china");
  assert.equal(test.normalizeSource("official"), "official");
  assert.match(test.downloadURLs("china", test.selectWheel("x86_64"))[0], /^https:\/\/pypi\.tuna\.tsinghua\.edu\.cn\/packages\//);
  assert.match(test.downloadURLs("china", test.selectWheel("x86_64"))[1], /^https:\/\/mirrors\.aliyun\.com\/pypi\/packages\//);
  assert.match(test.downloadURLs("official", test.selectWheel("x86_64"))[0], /^https:\/\/files\.pythonhosted\.org\/packages\//);
  assert.equal(test.indexURL("china"), "https://pypi.tuna.tsinghua.edu.cn/simple");
  assert.equal(test.indexURL("official"), "");

  const env = test.childEnvironment(harness.base, "china");
  assert.deepEqual(JSON.parse(JSON.stringify(env)), {
    UV_CACHE_DIR: harness.base + "\\cache",
    UV_PYTHON_INSTALL_DIR: harness.base + "\\python",
    UV_NO_CONFIG: "1",
    UV_LINK_MODE: "copy",
    UV_PYTHON_PREFERENCE: "only-managed",
    UV_PYTHON_INSTALL_MIRROR: "https://registry.npmmirror.com/-/binary/python-build-standalone",
    PYTHONUTF8: "1",
    PYTHONIOENCODING: "utf-8",
    NO_COLOR: "1",
  });
  assert.equal(test.childEnvironment(harness.base, "official").UV_PYTHON_INSTALL_MIRROR, null,
    "official source must clear an inherited Python mirror from the parent environment");
  assert.match(test.packageProbe, /version\('BabelDOC'\) == '0\.6\.4'/);
  assert.match(test.packageProbe, /version\('bitstring'\).*< 5/);
  assert.deepEqual(
    JSON.parse(JSON.stringify(test.buildStepPlan({ uvReady: true, pythonReady: true, environmentReady: true, babeldocReady: true }).map(step => step.skip))),
    [true, true, true, true, false],
  );
  assert.equal(test.shortError(new Error("Bearer secret-value"), "INFO install started\nERROR invalid sk-1234567890"), "ERROR invalid [已隐藏 API key]");
  assert.ok(test.shortError(new Error("x".repeat(1000), "")).length <= 600);
}

async function missingLocalAppDataFailsClearly() {
  const harness = makeHarness({ localAppData: "" });
  assert.throws(() => harness.installer.baseDir(), /LOCALAPPDATA/);
  assert.equal(await harness.installer.isReady(), false);
}

async function hashMismatchRejectsAndRemovesTheWheel() {
  const harness = makeHarness({ hashMismatch: true, promptChoices: [1] });
  const error = await harness.installer.ensureReady({ source: "china" }).then(
    () => null,
    value => value,
  );
  assert.match(error.message, /SHA-256 校验失败/);
  assert.equal(harness.prompts.length, 1);
  assert.equal(harness.prompts[0].retry, "重试");
  assert.equal(harness.prompts[0].cancel, "取消");
  assert.equal([...harness.files.keys()].some(file => file.endsWith("uv-0.13.1.whl")), false);
  assert.ok(harness.writes.some(([file]) => file.includes("install-") && file.endsWith(".log")));
}

async function concurrentInstallAndRetryResumeAtFailedStep() {
  const secret = "sk-installtestsecret123456";
  const harness = makeHarness({ failPipOnce: true, promptChoices: [0], apiKey: secret, outputSecret: secret });
  const progress = [];
  const first = harness.installer.ensureReady({ source: "china", onProgress: value => progress.push(value) });
  const second = harness.installer.ensureReady({ source: "official", onProgress: value => progress.push(value) });
  assert.equal(first, second, "concurrent callers should receive the same install promise");
  const pythonPath = await first;

  assert.equal(pythonPath, harness.base + "\\env\\Scripts\\python.exe");
  assert.equal(await harness.installer.isReady(), true);
  assert.equal(harness.prompts.length, 1, "the failed step should offer one retry");
  assert.match(harness.prompts[0].message, /步骤：安装 BabelDOC 0\.6\.4/);
  assert.equal(harness.prompts[0].message.includes("temporary package index failure"), true);
  assert.equal(harness.calls.filter(call => call.arguments[0] === "venv").length, 1, "retry resumes at the package-install step");
  assert.equal(harness.calls.filter(call => call.arguments[0] === "pip").length, 2);
  const uvPath = harness.installer._test.pathsFor(harness.base).uv;
  const uvInvocationsWithPreference = harness.calls.filter(call =>
    call.command === uvPath && call.environment.UV_PYTHON_PREFERENCE === "only-managed");
  assert.ok(uvInvocationsWithPreference.length > 0, "uv invocations should keep the managed-Python preference in their environment");
  assert.equal(uvInvocationsWithPreference.some(call => call.arguments.some(argument =>
    argument === "--managed-python" || argument === "--python-preference" || argument.startsWith("--python-preference="))), false,
  "uv preference environment must not be combined with conflicting CLI flags");
  assert.equal(harness.calls.some(call => call.arguments.includes("--index-url") && call.environment.UV_PYTHON_INSTALL_MIRROR), true);
  assert.equal(harness.calls.every(call => call.environment.UV_CACHE_DIR && call.environment.UV_NO_CONFIG === "1" && call.environment.PYTHONUTF8 === "1"), true);
  assert.ok(progress.some(value => value.stage === "准备翻译环境（第 1/5 步）：下载 uv"));
  assert.ok(progress.some(value => value.stepId === "download-uv" && value.stepCount === 5));
  const installJSON = JSON.parse(harness.textFiles.get(harness.base + "\\install.json"));
  assert.deepEqual(installJSON, {
    uv: "0.13.1", python: "3.12", babeldoc: "0.6.4", source: "china", completedAt: installJSON.completedAt,
  });
  assert.ok(Number.isFinite(Date.parse(installJSON.completedAt)));
  const installLog = harness.writes.find(([file, contents]) =>
    file.startsWith("C:\\Zotero\\babeldoc-for-zotero\\logs\\install-") && contents.includes("Successfully installed BabelDOC-0.6.4"));
  assert.ok(installLog, "a fresh install should write child output to the install log");
  assert.equal(installLog[1].includes(secret), false, "install logs must redact configured API keys");
}

async function stalePackageVersionIsReinstalled() {
  const harness = makeHarness({ wrongPackageVersion: true });
  await harness.installer.ensureReady({ source: "official" });
  assert.equal(harness.calls.filter(call => call.arguments[0] === "pip").length, 1,
    "a mismatched installed BabelDOC version should trigger the pinned package install");
  assert.equal(await harness.installer.isReady(), true);
}

async function repairDeletesTheFullBaseAndRunsEveryStage() {
  const harness = makeHarness({ locale: "en-US" });
  await harness.installer.ensureReady({ source: "china" });
  harness.promptChoice = 0;
  const existingLog = "C:\\Zotero\\babeldoc-for-zotero\\logs\\keep.log";
  const existingBabeldocCache = "C:\\Users\\Test\\.cache\\babeldoc\\fonts\\keep.bin";
  harness.textFiles.set(existingLog, "preserve this log");
  harness.files.set(existingBabeldocCache, true);
  const progress = [];
  const previousCallCount = harness.calls.length;
  const previousDownloadCount = harness.downloads.length;
  const previousRemoveCount = harness.removeCalls.length;
  const pythonPath = await harness.installer.repairEnvironment({
    source: "official",
    onProgress: value => progress.push(value),
  });

  assert.equal(pythonPath, harness.base + "\\env\\Scripts\\python.exe");
  assert.equal(harness.prompts.length, 1);
  assert.equal(harness.prompts[0].title, "Repair translation environment");
  assert.equal(harness.prompts[0].message, "This will delete and reinstall the translation environment (about 2 minutes). Continue?");
  assert.equal(harness.prompts[0].retry, "Reinstall");
  assert.equal(harness.prompts[0].cancel, "Cancel");
  assert.deepEqual(harness.localizationCalls, [
    "babeldoc-for-zotero-pref-environment-repair-confirm-title",
    "babeldoc-for-zotero-pref-environment-repair-confirm-message",
    "babeldoc-for-zotero-pref-environment-repair-confirm",
    "babeldoc-for-zotero-pref-environment-repair-cancel",
  ]);
  const baseRemovals = harness.removeCalls.slice(previousRemoveCount).filter(call => call.path === harness.base);
  assert.deepEqual(baseRemovals, [{
    path: harness.base,
    options: { recursive: true, ignoreAbsent: true },
  }], "repair should recursively remove only the exact managed base directory");
  assert.equal(harness.textFiles.get(existingLog), "preserve this log", "Zotero data-directory logs must survive repair");
  assert.equal(harness.files.has(existingBabeldocCache), true, "the BabelDOC user cache must survive repair");
  assert.equal(harness.downloads.length, previousDownloadCount + 1, "repair should download uv again after deleting its base");
  const repairCalls = harness.calls.slice(previousCallCount);
  assert.equal(repairCalls.filter(call => call.arguments[0] === "python" && call.arguments[1] === "install").length, 1);
  assert.equal(repairCalls.filter(call => call.arguments[0] === "venv").length, 1);
  assert.equal(repairCalls.filter(call => call.arguments[0] === "pip").length, 1);
  assert.equal(repairCalls.filter(call => call.arguments[0] === "--warmup").length, 1);
  assert.deepEqual(
    [...new Set(progress.map(value => value.stepId))].sort(),
    ["create-environment", "download-uv", "install-babeldoc", "install-python", "warmup"].sort(),
    "repair progress should cover all five install stages",
  );
  assert.equal(harness.maintenanceLocked, false, "the queue lease must release after repair completes");
}

async function normalInstallRepairsAMissingLauncher() {
  const harness = makeHarness({ existingEnvironment: true });
  const paths = harness.installer._test.pathsFor(harness.base);
  assert.equal(harness.textFiles.has(paths.installJSON), false, "the install manifest should be absent");
  assert.equal(harness.files.has(paths.python), true, "the Python environment should already exist");
  assert.equal(harness.files.has(paths.babeldoc), false, "the BabelDOC launcher should be missing");

  await harness.installer.ensureReady();

  const packageInstall = harness.calls.find(call => call.arguments[0] === "pip");
  assert.ok(packageInstall, "a successful package import without its launcher should trigger package installation");
  assert.deepEqual(
    Array.from(packageInstall.arguments.slice(0, 5)),
    ["pip", "install", "--reinstall-package", "BabelDOC", "--python"],
    "uv should reinstall BabelDOC even when package metadata says it is already installed",
  );
  assert.equal(harness.files.has(paths.babeldoc), true, "the simulated uv reinstall should restore the launcher");
  assert.equal(harness.calls.filter(call => call.arguments[0] === "venv").length, 0,
    "repairing the launcher should preserve the existing Python environment");
  assert.equal(harness.calls.filter(call => call.arguments[0] === "--warmup").length, 1,
    "warmup should run after the reinstall recreates the launcher");
}

async function backgroundFailureDoesNotPromptAndNormalInstallResumes() {
  const harness = makeHarness({ failPipOnce: true });
  const error = await harness.installer.ensureReady({ interactive: false }).then(
    () => null,
    value => value,
  );

  assert.ok(error, "a failed background install should reject for its caller to report non-modally");
  assert.ok(error.logPath, "the failure should retain the diagnostic log path");
  assert.equal(harness.prompts.length, 0, "a non-interactive install must not call confirmEx");

  await harness.installer.ensureReady();
  assert.equal(harness.calls.filter(call => call.arguments[0] === "venv").length, 1,
    "a normal ensureReady call must resume the partial environment instead of deleting it");
  assert.equal(harness.calls.filter(call => call.arguments[0] === "pip").length, 2,
    "a later action should resume the failed package-install step");
}

async function repairRefusesWhileTranslationIsRunning() {
  const harness = makeHarness({ queueBusy: true });
  const error = await harness.installer.repairEnvironment().then(() => null, value => value);
  assert.match(error.message, /翻译正在运行，无法修复翻译环境/);
  assert.equal(harness.prompts.length, 0);
  assert.equal(harness.removeCalls.length, 0);
  assert.equal(harness.calls.length, 0);
  assert.equal(harness.downloads.length, 0);
}

async function repairAbortsAndWaitsForAnInstallBeforeDeleting() {
  const harness = makeHarness({ blockWarmup: true });
  const startupInstall = harness.installer.ensureReady({ interactive: false });
  await harness.warmupStarted.promise;
  const repair = harness.installer.repairEnvironment();
  const startupError = await startupInstall.then(() => null, value => value);
  assert.ok(startupError, "repair should abort the installer that was already running");
  const pythonPath = await repair;
  assert.equal(pythonPath, harness.base + "\\env\\Scripts\\python.exe");
  assert.equal(harness.killCount, 1, "repair should kill the active install child before deleting the directory");
  const childSettledAt = harness.events.indexOf("blocked-install-child-settled");
  const directoryRemovedAt = harness.events.indexOf("base-directory-removed");
  assert.ok(childSettledAt !== -1 && directoryRemovedAt > childSettledAt,
    "repair must wait for the underlying child process to settle before removing its files");
  assert.equal(await harness.installer.isReady(), true);
}

async function repairSignalCancellationBeforeDeletionLeavesTheBaseUntouched() {
  const harness = makeHarness({ existingEnvironment: true, blockReadinessAt: 1 });
  const controller = new AbortController();
  const repair = harness.installer.repairEnvironment({ signal: controller.signal });
  await harness.readinessCheckStarted.promise;

  controller.abort();
  harness.completeReadiness();
  const error = await repair.then(() => null, value => value);
  assert.match(error.message, /修复环境已取消/);
  assert.equal(harness.removeCalls.length, 0, "an aborted repair must not delete the base directory");
  assert.equal(harness.calls.length, 0, "an aborted repair must not start installation");
  assert.equal(harness.downloads.length, 0);
}

async function shutdownDuringPostInstallReadinessCheckDoesNotDeleteTheBase() {
  const harness = makeHarness({ existingEnvironment: true, blockReadinessAt: 2 });
  const paths = harness.installer._test.pathsFor(harness.base);
  const repair = harness.installer.repairEnvironment();
  harness.textFiles.set(paths.installJSON, JSON.stringify({
    uv: "0.13.1", python: "3.12", babeldoc: "0.6.4",
  }));
  await harness.readinessCheckStarted.promise;

  harness.installer.cancelInFlight();
  harness.completeReadiness();
  const error = await repair.then(() => null, value => value);
  assert.match(error.message, /installer is inactive/);
  assert.equal(harness.removeCalls.length, 0, "shutdown during the second readiness check must not delete the base");
  assert.equal(harness.calls.length, 0, "shutdown must not start a replacement install");
  assert.equal(harness.downloads.length, 0);
}

async function repairPathGuardRejectsOtherDirectories() {
  const harness = makeHarness();
  const error = await harness.installer._test.removeManagedBase("C:\\Users\\Test\\AppData\\Local\\other").then(
    () => null,
    value => value,
  );
  assert.match(error.message, /拒绝删除非 BabelDOC 管理环境目录/);
  assert.equal(harness.removeCalls.length, 0);
}

async function cancelingRepairLeavesFilesAndInstallCallsUntouched() {
  const harness = makeHarness();
  await harness.installer.ensureReady();
  harness.promptChoice = 1;
  const filesBefore = [...harness.files.entries()];
  const textFilesBefore = [...harness.textFiles.entries()];
  const callsBefore = harness.calls.length;
  const downloadsBefore = harness.downloads.length;
  const removalsBefore = harness.removeCalls.length;
  const result = await harness.installer.repairEnvironment();

  assert.equal(result, null, "Cancel should end the repair without starting installation");
  assert.deepEqual([...harness.files.entries()], filesBefore);
  assert.deepEqual([...harness.textFiles.entries()], textFilesBefore);
  assert.equal(harness.calls.length, callsBefore);
  assert.equal(harness.downloads.length, downloadsBefore);
  assert.equal(harness.removeCalls.length, removalsBefore);
  assert.equal(harness.prompts.length, 1);
  assert.equal(harness.prompts[0].retry, "重新安装");
  assert.equal(harness.prompts[0].cancel, "取消");
  assert.equal(harness.maintenanceLocked, false);
}

async function failedBaseDeletionBlocksOtherInstallEntrypoints() {
  const harness = makeHarness({ failBaseRemoval: true });
  await harness.installer.ensureReady();
  harness.promptChoice = 0;
  const callsBefore = harness.calls.length;
  const error = await harness.installer.repairEnvironment().then(() => null, value => value);
  assert.match(error.message, /文件可能正在使用；未开始重新安装/);
  const followupError = await harness.installer.ensureReady().then(() => null, value => value);
  assert.equal(followupError, error, "ensureReady must preserve the deletion failure instead of writing into the base");
  assert.equal(harness.calls.length, callsBefore);
  assert.equal(harness.maintenanceLocked, false);
}

async function translationJoinsBackgroundInstall() {
  const harness = makeHarness({ blockWarmup: true });
  const backgroundInstall = harness.installer.ensureReady({ interactive: false });
  await harness.warmupStarted.promise;

  const translationInstall = harness.installer.ensureReady({ source: "china" });
  assert.equal(translationInstall, backgroundInstall,
    "translation should wait on the background install already in flight");

  harness.completeWarmup();
  assert.equal(await translationInstall, harness.base + "\\env\\Scripts\\python.exe");
  assert.equal(harness.calls.filter(call => call.arguments[0] === "--warmup").length, 1,
    "joining the background attempt must not start another warmup child");
}

async function cancelledInteractiveWaiterDoesNotPromptForBackgroundInstall() {
  const harness = makeHarness({ blockWarmup: true });
  const translationController = new AbortController();
  const translationInstall = harness.installer.ensureReady({ signal: translationController.signal });
  await harness.warmupStarted.promise;

  const backgroundInstall = harness.installer.ensureReady({ interactive: false });
  translationController.abort();
  const translationError = await translationInstall.then(() => null, error => error);
  assert.ok(translationError, "the cancelled translation waiter should reject");
  assert.equal(harness.killCount, 0, "the background waiter must keep the shared child alive");

  harness.completeWarmup(1);
  const backgroundError = await backgroundInstall.then(() => null, error => error);
  assert.ok(backgroundError);
  assert.equal(harness.prompts.length, 0,
    "a cancelled interactive waiter must not make the remaining background attempt prompt");
}

async function shutdownCancelsAnInstallWithUncancellableWaiters() {
  const harness = makeHarness({ blockWarmup: true });
  const settingsInstall = harness.installer.ensureReady();
  await harness.warmupStarted.promise;
  const translationInstall = harness.installer.ensureReady({ signal: new AbortController().signal });

  harness.installer.cancelInFlight();
  const [settingsError, translationError] = await Promise.all([
    settingsInstall.then(() => null, error => error),
    translationInstall.then(() => null, error => error),
  ]);
  assert.ok(settingsError, "shutdown should release a settings waiter that has no signal");
  assert.ok(translationError, "shutdown should release other waiters too");
  assert.equal(harness.killCount, 1, "global cancellation should stop the shared child despite active waiters");

  harness.installer.activate();
  await harness.installer.ensureReady();
  assert.equal(await harness.installer.isReady(), true, "a later startup can restart after cleanup");
}

async function shutdownInvalidatesAReadinessContinuationUntilReactivated() {
  const harness = makeHarness({ blockReadiness: true });
  const pendingInstall = harness.installer.ensureReady();
  await harness.readinessCheckStarted.promise;

  harness.installer.cancelInFlight();
  const inactiveError = await harness.installer.ensureReady().then(() => null, error => error);
  assert.ok(inactiveError, "new calls must remain blocked until startup reactivates the installer");
  assert.equal(harness.calls.length, 0);

  harness.installer.activate();
  const reactivatedInstall = harness.installer.ensureReady();
  harness.completeReadiness();
  const shutdownError = await pendingInstall.then(() => null, error => error);
  assert.ok(shutdownError, "a readiness continuation from the previous lifecycle must stay invalid after reactivation");
  await reactivatedInstall;
  assert.equal(harness.calls.filter(call => call.arguments[0] === "venv").length, 1,
    "the reactivated caller should start one fresh attempt after the old check settles");
}

async function shutdownInvalidatesAWaitForRestartContinuation() {
  const harness = makeHarness({ blockWarmup: true });
  const controller = new AbortController();
  const cancelledInstall = harness.installer.ensureReady({ signal: controller.signal });
  await harness.warmupStarted.promise;

  controller.abort();
  const restartWaiter = harness.installer.ensureReady();
  harness.installer.cancelInFlight();
  harness.installer.activate();
  const reactivatedWaiter = harness.installer.ensureReady();
  await cancelledInstall.then(() => null, error => error);
  const restartError = await restartWaiter.then(() => null, error => error);
  assert.ok(restartError, "a waiter from the previous lifecycle must not restart a cancelled install");
  await reactivatedWaiter;
  assert.equal(harness.calls.filter(call => call.arguments[0] === "--warmup").length, 2,
    "only the fresh waiter may restart after reactivation");

  assert.equal(await harness.installer.isReady(), true,
    "the reactivated installer should leave a usable environment");
}

function makeActiveV1Installer() {
  let installing = true;
  let listener = null;
  let listenerRemovals = 0;
  let oldEnsureReadyCalls = 0;
  const installer = {
    __moduleVersion: 1,
    internalController: { signal: { aborted: true } },
    isInstalling() { return installing; },
    addStateListener(callback) {
      listener = callback;
      callback(installing);
      return () => {
        listenerRemovals++;
        listener = null;
      };
    },
    ensureReady() {
      oldEnsureReadyCalls++;
      return Promise.reject(new Error("the aborted v1 task must not be restarted"));
    },
  };
  return {
    installer,
    finish() {
      installing = false;
      if (listener) listener(false);
    },
    get listenerRemovals() { return listenerRemovals; },
    get oldEnsureReadyCalls() { return oldEnsureReadyCalls; },
  };
}

async function v2WaitsPassivelyForAnActiveV1Singleton() {
  const previous = makeActiveV1Installer();
  const harness = makeHarness({ previousInstaller: previous.installer });
  const install = harness.installer.ensureReady();
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(previous.oldEnsureReadyCalls, 0,
    "the handoff must not call ensureReady on an aborted v1 task");
  assert.equal(harness.calls.length, 0, "v2 must wait before writing the environment");
  assert.equal(harness.writes.length, 0, "v2 must not write logs or manifests while v1 is active");
  previous.finish();
  await install;
  assert.ok(harness.calls.length > 0, "v2 should start after the old singleton finishes");
  assert.equal(previous.listenerRemovals, 1, "the v1 state listener should be detached after completion");
  assert.equal(previous.oldEnsureReadyCalls, 0);

  let idleOldEnsureReadyCalls = 0;
  const inactiveLegacyHarness = makeHarness({ previousInstaller: {
    __moduleVersion: 1,
    isInstalling() { return false; },
    ensureReady() {
      idleOldEnsureReadyCalls++;
      throw new Error("must not start work through v1");
    },
  } });
  await inactiveLegacyHarness.installer.ensureReady();
  assert.equal(idleOldEnsureReadyCalls, 0,
    "module upgrade must not invoke the old singleton when it has no active task");
}

async function legacyBarrierWaitersCancelPromptlyWithoutDroppingObservation() {
  const previous = makeActiveV1Installer();
  const harness = makeHarness({ previousInstaller: previous.installer });
  const controller = new AbortController();
  const startupWaiter = harness.installer.ensureReady({ signal: controller.signal });
  await new Promise(resolve => setImmediate(resolve));

  controller.abort();
  const startupError = await startupWaiter.then(() => null, error => error);
  assert.ok(startupError, "an aborted startup waiter should stop waiting for the old installer");
  assert.equal(previous.listenerRemovals, 0,
    "cancelling one waiter must leave the shared v1 state observation active");

  const settingsWaiter = harness.installer.ensureReady();
  harness.installer.cancelInFlight();
  const shutdownError = await settingsWaiter.then(() => null, error => error);
  assert.ok(shutdownError, "shutdown should promptly release a settings waiter without its own signal");
  assert.equal(harness.calls.length, 0, "canceled v2 waiters must not start environment work");
  assert.equal(previous.listenerRemovals, 0, "shutdown must not detach the shared observation prematurely");

  previous.finish();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(previous.listenerRemovals, 1, "the shared observation should detach when v1 becomes idle");
  assert.equal(previous.oldEnsureReadyCalls, 0);

  harness.installer.activate();
  await harness.installer.ensureReady();
  assert.ok(harness.calls.length > 0, "a later startup should proceed after the passive handoff completes");
}

async function aFailedLegacyListenerSetupDoesNotStartV2Work() {
  let oldEnsureReadyCalls = 0;
  const harness = makeHarness({ previousInstaller: {
    __moduleVersion: 1,
    isInstalling() { return true; },
    addStateListener() { throw new Error("legacy state observer unavailable"); },
    ensureReady() {
      oldEnsureReadyCalls++;
      throw new Error("must not start old installer work");
    },
  } });
  const pendingInstall = harness.installer.ensureReady();
  await new Promise(resolve => setImmediate(resolve));

  assert.equal(harness.calls.length, 0, "v2 must fail closed when it cannot observe an active v1 task");
  assert.equal(harness.writes.length, 0);
  harness.installer.cancelInFlight();
  const error = await pendingInstall.then(() => null, value => value);
  assert.ok(error, "shutdown should release the waiter even when the v1 observer could not be attached");
  assert.equal(oldEnsureReadyCalls, 0);
}

async function cancellationKillsTheRunningChild() {
  const harness = makeHarness({ blockWarmup: true });
  const controller = new AbortController();
  const install = harness.installer.ensureReady({ signal: controller.signal });
  await harness.warmupStarted.promise;
  controller.abort();
  const error = await install.then(() => null, value => value);
  assert.equal(harness.killCount, 1);
  assert.equal(error.message, "翻译任务已取消。");
  assert.equal(harness.prompts.length, 0, "cancellation should not ask whether to retry");
}

async function cancelledDownloadReleasesInFlightAfterLateCancellerRegistration() {
  const downloadStarted = deferred();
  const harness = makeHarness({ stallDownload: true });
  harness.setDownloadStarted(downloadStarted);
  const install = harness.installer.ensureReady();
  const registerCanceller = await downloadStarted.promise;
  const downloadOptions = harness.downloads[0].downloadOptions;

  assert.equal(downloadOptions.errorDelayMax, 0,
    "the installer should disable Zotero's automatic 5xx retry loop");
  assert.equal(downloadOptions.noRetryOnThrottle, true,
    "the installer should leave 429 and 503 Retry-After handling to its own retry flow");

  harness.installer.cancelInFlight();
  harness.installer.activate();
  const restartedInstall = harness.installer.ensureReady();
  registerCanceller();

  const cancelledError = await within(
    install.then(() => null, error => error),
    "the cancelled installer remained stuck after the late canceller was registered",
  );
  assert.ok(cancelledError, "the cancelled download should reject after its late canceller is registered");
  await within(restartedInstall, "the re-enabled installer remained blocked by stale inFlight state");
  assert.equal(harness.downloadCancelCount, 1,
    "a canceller registered after abort must immediately cancel Zotero's download");
  assert.equal(harness.downloads.length, 2,
    "the re-enabled installer should start a fresh download after the cancelled attempt settles");
}

async function cancellingOneWaiterDoesNotStopASettingsInstall() {
  const harness = makeHarness({ blockWarmup: true });
  const settingsInstall = harness.installer.ensureReady();
  await harness.warmupStarted.promise;

  const controller = new AbortController();
  const jobInstall = harness.installer.ensureReady({ signal: controller.signal });
  controller.abort();
  let timeout;
  const jobResult = await Promise.race([
    jobInstall.then(value => ({ value }), error => ({ error })),
    new Promise(resolve => { timeout = setTimeout(() => resolve({ pending: true }), 200); }),
  ]);
  clearTimeout(timeout);

  assert.equal(jobResult.pending, undefined, "the cancelled job must stop waiting promptly");
  assert.equal(jobResult.error && jobResult.error.message, "翻译任务已取消。");
  assert.equal(harness.killCount, 0, "the settings caller still needs the shared child");

  harness.completeWarmup();
  assert.equal(await settingsInstall, harness.base + "\\env\\Scripts\\python.exe");
  assert.equal(harness.killCount, 0);
}

async function aNewCallerRestartsAfterEveryWaiterCancels() {
  const harness = makeHarness({ blockWarmup: true });
  const controller = new AbortController();
  const cancelledInstall = harness.installer.ensureReady({ signal: controller.signal });
  await harness.warmupStarted.promise;

  controller.abort();
  const retryInstall = harness.installer.ensureReady();
  const cancelError = await cancelledInstall.then(() => null, value => value);
  assert.equal(cancelError.message, "翻译任务已取消。");
  assert.equal(harness.killCount, 1);
  assert.equal(await retryInstall, harness.base + "\\env\\Scripts\\python.exe");
  assert.equal(await harness.installer.isReady(), true);
}

async function environmentCheckReportsAndCancels() {
  const harness = makeHarness({ existingEnvironment: true });
  const paths = harness.installer._test.pathsFor(harness.base);
  harness.files.set(paths.python, true);
  harness.textFiles.set(paths.installJSON, JSON.stringify({
    uv: "0.13.1", python: "3.12", babeldoc: "0.6.4",
  }));

  const healthy = await harness.installer.checkEnvironment();
  assert.equal(healthy.ok, true, JSON.stringify(healthy.checks));
  assert.equal(healthy.checks.length, 7);
  assert.ok(healthy.checks.every(item => item.status === "pass"), JSON.stringify(healthy.checks));

  const callsBefore = harness.calls.length;
  const installsBefore = harness.downloads.length;
  harness.textFiles.set(paths.installJSON, JSON.stringify({
    uv: "0.13.1", python: "3.12", babeldoc: "0.6.3",
  }));
  const staleInstall = await harness.installer.checkEnvironment();
  assert.equal(staleInstall.ok, false);
  assert.equal(staleInstall.checks.find(item => item.id === "install").status, "fail");
  assert.equal(harness.calls.length, callsBefore, "check must not run install steps");
  assert.equal(harness.downloads.length, installsBefore, "check must not download anything");

  const brokenHarness = makeHarness({
    existingEnvironment: true, checkBabeldocVersion: "0.6.3",
  });
  const brokenPaths = brokenHarness.installer._test.pathsFor(brokenHarness.base);
  brokenHarness.files.set(brokenPaths.python, true);
  brokenHarness.textFiles.set(brokenPaths.installJSON, JSON.stringify({
    uv: "0.13.1", python: "3.12", babeldoc: "0.6.4",
  }));
  const broken = await brokenHarness.installer.checkEnvironment();
  assert.equal(broken.ok, false);
  assert.equal(broken.checks.find(item => item.id === "babeldoc").status, "fail");
  assert.equal(brokenHarness.downloads.length, 0, "check must never download");

  const controller = new AbortController();
  controller.abort();
  const cancelled = await harness.installer.checkEnvironment({ signal: controller.signal })
    .then(() => null, value => value);
  assert.match(cancelled.message, /环境检测已取消/);

  harness.textFiles.set(paths.installJSON, JSON.stringify({
    uv: "0.13.1", python: "3.12", babeldoc: "0.6.4",
  }));
  const probeCallsBefore = harness.calls.length;
  const first = harness.installer.checkEnvironment();
  const second = harness.installer.checkEnvironment();
  assert.equal(second, first, "concurrent checks share one run");
  const [one, two] = await Promise.all([first, second]);
  assert.equal(one.ok, true);
  assert.equal(two, one);
  assert.equal(harness.calls.length - probeCallsBefore, 2,
    "one version probe and one smoke probe for a healthy environment");

  const missingHarness = makeHarness({
    existingEnvironment: true, checkFonts: 0, checkModels: 0, failSmokeCheck: true,
  });
  const missingPaths = missingHarness.installer._test.pathsFor(missingHarness.base);
  missingHarness.files.set(missingPaths.python, true);
  missingHarness.textFiles.set(missingPaths.installJSON, JSON.stringify({
    uv: "0.13.1", python: "3.12", babeldoc: "0.6.4",
  }));
  const missing = await missingHarness.installer.checkEnvironment();
  assert.equal(missing.ok, false);
  assert.equal(missing.checks.find(item => item.id === "fonts").status, "fail");
  assert.equal(missing.checks.find(item => item.id === "models").status, "fail");
  assert.equal(missing.checks.find(item => item.id === "smoke").status, "fail");
  assert.ok(missing.log && missing.log.length > 0, "diagnostic log is returned");
  assert.equal(missingHarness.downloads.length, 0, "missing resources never trigger downloads");
}

(async () => {
  await pureConfigurationAndReadiness();
  await missingLocalAppDataFailsClearly();
  await hashMismatchRejectsAndRemovesTheWheel();
  await concurrentInstallAndRetryResumeAtFailedStep();
  await stalePackageVersionIsReinstalled();
  await repairDeletesTheFullBaseAndRunsEveryStage();
  await normalInstallRepairsAMissingLauncher();
  await backgroundFailureDoesNotPromptAndNormalInstallResumes();
  await repairRefusesWhileTranslationIsRunning();
  await repairAbortsAndWaitsForAnInstallBeforeDeleting();
  await repairSignalCancellationBeforeDeletionLeavesTheBaseUntouched();
  await shutdownDuringPostInstallReadinessCheckDoesNotDeleteTheBase();
  await repairPathGuardRejectsOtherDirectories();
  await cancelingRepairLeavesFilesAndInstallCallsUntouched();
  await failedBaseDeletionBlocksOtherInstallEntrypoints();
  await translationJoinsBackgroundInstall();
  await cancelledInteractiveWaiterDoesNotPromptForBackgroundInstall();
  await shutdownCancelsAnInstallWithUncancellableWaiters();
  await shutdownInvalidatesAReadinessContinuationUntilReactivated();
  await shutdownInvalidatesAWaitForRestartContinuation();
  await v2WaitsPassivelyForAnActiveV1Singleton();
  await legacyBarrierWaitersCancelPromptlyWithoutDroppingObservation();
  await aFailedLegacyListenerSetupDoesNotStartV2Work();
  await cancellingOneWaiterDoesNotStopASettingsInstall();
  await aNewCallerRestartsAfterEveryWaiterCancels();
  await cancellationKillsTheRunningChild();
  await cancelledDownloadReleasesInFlightAfterLateCancellerRegistration();

  await environmentCheckReportsAndCancels();
  process.stdout.write("installer regression cases passed\n");
})().catch(error => {
  process.stderr.write((error && error.stack) || String(error));
  process.exitCode = 1;
});
