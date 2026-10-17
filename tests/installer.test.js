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
  const writes = [];
  const root = options.localAppData === undefined
    ? "C:\\Users\\Test\\AppData\\Local"
    : options.localAppData;
  const dirs = ["C:\\Zotero"];
  if (root) dirs.push(root);
  const base = root ? root + "\\babeldoc-for-zotero" : "";
  const join = (...parts) => parts.filter(Boolean).join("\\").replace(/\\{2,}/g, "\\");
  const warmupStarted = deferred();
  const warmupWait = deferred();
  let pythonInstalled = false;
  let environmentCreated = false;
  let packageInstalled = !!options.wrongPackageVersion;
  let wrongPackageVersion = !!options.wrongPackageVersion;
  let failedPip = false;
  let killCount = 0;
  let warmupWasBlocked = false;
  let promptChoice = 1;

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
      files.delete(filePath);
      textFiles.delete(filePath);
      if (removeOptions.recursive) {
        for (const key of [...files.keys()]) if (key.startsWith(filePath + "\\")) files.delete(key);
        for (const key of [...textFiles.keys()]) if (key.startsWith(filePath + "\\")) textFiles.delete(key);
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
              warmupWait.resolve({ exitCode: -9 });
            },
            wait() { return warmupWait.promise; },
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
    getMainWindow() { return { main: true }; },
    HTTP: {
      async download(url, destination, downloadOptions) {
        downloads.push({ url, destination, downloadOptions });
        if (options.stallDownload) {
          return new Promise((resolve, reject) => {
            downloadOptions.cancellerReceiver(() => reject(new Error("download aborted")));
            options.downloadStarted.resolve();
          });
        }
        downloadOptions.onProgress(3, 3);
        files.set(destination, true);
      },
    },
  };

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
    writes,
    dirs,
    base,
    warmupStarted,
    completeWarmup() { warmupWait.resolve({ exitCode: 0 }); },
    set promptChoice(value) { promptChoice = value; },
    get killCount() { return killCount; },
    setDownloadStarted(value) { options.downloadStarted = value; },
  };
}

async function pureConfigurationAndReadiness() {
  const harness = makeHarness();
  const test = harness.installer._test;
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
    JSON.parse(JSON.stringify(test.buildStepPlan({ uvReady: true, pythonReady: true, environmentReady: true, babeldocReady: true }, false).map(step => step.skip))),
    [true, true, true, true, false],
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(test.buildStepPlan({ uvReady: true, pythonReady: true, environmentReady: true, babeldocReady: true }, true).map(step => step.skip))),
    [true, true, false, false, false],
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

(async () => {
  await pureConfigurationAndReadiness();
  await missingLocalAppDataFailsClearly();
  await hashMismatchRejectsAndRemovesTheWheel();
  await concurrentInstallAndRetryResumeAtFailedStep();
  await stalePackageVersionIsReinstalled();
  await cancellingOneWaiterDoesNotStopASettingsInstall();
  await aNewCallerRestartsAfterEveryWaiterCancels();
  await cancellationKillsTheRunningChild();
  process.stdout.write("installer regression cases passed\n");
})().catch(error => {
  process.stderr.write((error && error.stack) || String(error));
  process.exitCode = 1;
});
