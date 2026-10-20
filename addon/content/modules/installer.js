/* global Zotero, ZoteroTranslateFilesConfig, Services, IOUtils, PathUtils, ChromeUtils, Cc, Ci, crypto */

var installerPriorSingleton = Zotero.__babeldocForZoteroInstaller;
var installerPriorBarrier = null;
var installerPriorBarrierFinish = null;
if (installerPriorSingleton && installerPriorSingleton.__moduleVersion !== 3 &&
    typeof installerPriorSingleton.isInstalling === "function") {
  try {
    if (installerPriorSingleton.isInstalling()) {
      installerPriorBarrier = new Promise(function (resolve) {
        var removeListener = null;
        var finished = false;
        var detachAfterRegistration = false;
        function finishWhenIdle() {
          if (finished) return;
          finished = true;
          if (typeof removeListener === "function") {
            try { removeListener(); } catch (error) { /* The handoff must still finish. */ }
          } else {
            detachAfterRegistration = true;
          }
          resolve();
        }
        installerPriorBarrierFinish = finishWhenIdle;
        try {
          if (typeof installerPriorSingleton.addStateListener !== "function") return;
          removeListener = installerPriorSingleton.addStateListener(function (running) {
            if (!running) finishWhenIdle();
          });
          if (detachAfterRegistration && typeof removeListener === "function") {
            try { removeListener(); } catch (error) { /* The handoff must still finish. */ }
          }
          if (!installerPriorSingleton.isInstalling()) finishWhenIdle();
        } catch (error) {
          try {
            if (!installerPriorSingleton.isInstalling()) finishWhenIdle();
          } catch (stateError) {
            // Stay blocked if the old install state cannot be observed safely.
          }
        }
      });
    }
  } catch (error) {
    installerPriorBarrier = new Promise(function () {});
  }
}

var ZoteroTranslateFilesInstaller = installerPriorSingleton &&
  installerPriorSingleton.__moduleVersion === 3
  ? installerPriorSingleton
  : (function (initialInstallBarrier) {
  var UV_VERSION = "0.13.1";
  var PYTHON_VERSION = "3.12";
  var BABELDOC_VERSION = "0.6.4";
  var PREFERENCE_PREFIX = "extensions.babeldoc-for-zotero.";
  var TUNA_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple";
  var PYTHON_MIRROR = "https://registry.npmmirror.com/-/binary/python-build-standalone";
  var PACKAGE_PROBE = "import babeldoc.main; from importlib.metadata import version; assert version('BabelDOC') == '" + BABELDOC_VERSION + "'; assert int(version('bitstring').split('.')[0]) < 5";
  var WHEELS = {
    amd64: {
      path: "packages/2a/8e/d23278bb327d2a75dc1d033c81b7fe23bbb28e363e02e7f306abd047924a/uv-0.13.1-py3-none-win_amd64.whl",
      sha256: "4f693cd4feb52f08f30474c4612f24be2050472609be65b9960b635f286da284",
    },
    arm64: {
      path: "packages/0a/e0/f27f161ee2ad242cb714fe7185f63cc2a88b83c4d8b375ca8c580f9f23b7/uv-0.13.1-py3-none-win_arm64.whl",
      sha256: "b173413a5dc18437b2d28266a02be636f35ac7e97741773aed163ed2143abfaf",
    },
  };
  var STEP_NAMES = ["下载 uv", "安装 Python 3.12", "创建环境", "安装 BabelDOC 0.6.4", "下载字体和模型"];
  var STEP_IDS = ["download-uv", "install-python", "create-environment", "install-babeldoc", "warmup"];
  var inFlight = null;
  var inFlightAbortController = null;
  var inFlightWaiters = [];
  var progressObservers = [];
  var stateObservers = [];
  var activeInstall = false;
  var acceptingInstalls = true;
  var lifecycleGeneration = 0;
  var legacyInstallBarrier = initialInstallBarrier;
  var legacyInstallWaiters = [];
  var repairOperation = null;
  var repairDeletionError = null;

  function redact(value) {
    var text = String(value == null ? "" : value);
    if (ZoteroTranslateFilesConfig && typeof ZoteroTranslateFilesConfig.redact === "function") {
      return ZoteroTranslateFilesConfig.redact(text);
    }
    return text.replace(/Bearer\s+\S+/gi, "Bearer [已隐藏]").replace(/sk-[A-Za-z0-9_-]{8,}/g, "[已隐藏 API key]");
  }

  function normalizeSource(source) {
    return source === "official" ? "official" : "china";
  }

  function isExpectedUVVersion(output) {
    var match = String(output || "").trim().match(/^uv\s+(\d+\.\d+\.\d+)(?:\s|$)/);
    return !!match && match[1] === UV_VERSION;
  }

  function selectWheel(architecture) {
    var arch = String(architecture || "").toLowerCase().replace(/[\s-]/g, "_");
    if (arch === "x86_64" || arch === "amd64") return WHEELS.amd64;
    if (arch === "aarch64" || arch === "arm64") return WHEELS.arm64;
    throw new Error("不支持的处理器架构：" + String(architecture || "未知") + "。目前仅支持 Windows x86-64 和 ARM64。");
  }

  function downloadURLs(source, wheel) {
    var hosts = source === "china"
      ? ["https://pypi.tuna.tsinghua.edu.cn/", "https://mirrors.aliyun.com/pypi/"]
      : ["https://files.pythonhosted.org/"];
    return hosts.map(function (host) { return host + wheel.path; });
  }

  function childEnvironment(base, source) {
    var env = {
      UV_CACHE_DIR: PathUtils.join(base, "cache"),
      UV_PYTHON_INSTALL_DIR: PathUtils.join(base, "python"),
      UV_NO_CONFIG: "1",
      UV_LINK_MODE: "copy",
      UV_PYTHON_PREFERENCE: "only-managed",
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8",
      NO_COLOR: "1",
    };
    env.UV_PYTHON_INSTALL_MIRROR = source === "china" ? PYTHON_MIRROR : null;
    return env;
  }

  function buildStepPlan(state) {
    state = state || {};
    return STEP_NAMES.map(function (name, index) {
      var complete = index === 0 ? !!state.uvReady
        : index === 1 ? !!state.pythonReady
          : index === 2 ? !!state.environmentReady
            : index === 3 ? !!state.babeldocReady
              : false;
      return { index: index + 1, name: name, skip: complete };
    });
  }

  function join() {
    return PathUtils.join.apply(PathUtils, arguments);
  }

  function fileExists(path) {
    try {
      var file = Zotero.File.pathToFile(path);
      return file.exists() && file.isFile();
    } catch (error) {
      return false;
    }
  }

  function baseDir() {
    var localAppData = "";
    try {
      localAppData = String(Services.env.get("LOCALAPPDATA") || "").trim();
    } catch (error) {
      localAppData = "";
    }
    if (!localAppData) {
      throw new Error("无法读取 Windows 的 LOCALAPPDATA 环境变量，不能创建 BabelDOC 翻译环境。");
    }
    return join(localAppData, "babeldoc-for-zotero");
  }

  function pathsFor(base) {
    var environment = join(base, "env");
    return {
      base: base,
      uvDir: join(base, "uv"),
      uv: join(base, "uv", "uv.exe"),
      pythonInstallDir: join(base, "python"),
      cache: join(base, "cache"),
      environment: environment,
      python: join(environment, "Scripts", "python.exe"),
      babeldoc: join(environment, "Scripts", "babeldoc.exe"),
      installJSON: join(base, "install.json"),
    };
  }

  function normalizedWindowsPath(path) {
    return String(path || "").replace(/\//g, "\\").replace(/\\+/g, "\\")
      .replace(/\\+$/, "").toLowerCase();
  }

  function windowsPathsOverlap(first, second) {
    var left = normalizedWindowsPath(first);
    var right = normalizedWindowsPath(second);
    return left === right || left.startsWith(right + "\\") || right.startsWith(left + "\\");
  }

  async function removeManagedBase(path) {
    var expected = pathsFor(baseDir()).base;
    if (!path || normalizedWindowsPath(path) !== normalizedWindowsPath(expected) ||
        !normalizedWindowsPath(expected).endsWith("\\babeldoc-for-zotero")) {
      throw new Error("拒绝删除非 BabelDOC 管理环境目录。");
    }
    var zoteroLogs = join(Zotero.DataDirectory.dir, "babeldoc-for-zotero", "logs");
    if (windowsPathsOverlap(expected, zoteroLogs)) {
      throw repairError("ENVIRONMENT_DELETE_PROTECTED", "翻译环境目录与 Zotero 日志目录重叠；为保护日志，未删除环境，也未开始重新安装。");
    }
    await IOUtils.remove(expected, { recursive: true, ignoreAbsent: true });
  }

  async function isReady() {
    var base;
    try {
      base = baseDir();
    } catch (error) {
      return false;
    }
    var paths = pathsFor(base);
    if (!fileExists(paths.python)) return false;
    try {
      var contents = await IOUtils.readUTF8(paths.installJSON);
      var record = JSON.parse(contents);
      return record.uv === UV_VERSION && record.python === PYTHON_VERSION &&
        record.babeldoc === BABELDOC_VERSION && fileExists(paths.python);
    } catch (error) {
      return false;
    }
  }

  var environmentCheck = null;

  var CHECK_PYTHON_SCRIPT = [
    "import importlib.metadata, sys",
    "from pathlib import Path",
    "print('python=' + '.'.join(map(str, sys.version_info[:2])))",
    "print('babeldoc=' + importlib.metadata.version('BabelDOC'))",
    "print('bitstring=' + importlib.metadata.version('bitstring'))",
    "fonts = Path.home() / '.cache' / 'babeldoc' / 'fonts'",
    "print('fonts=' + str(len(list(fonts.glob('*.ttf'))) if fonts.is_dir() else 0))",
    "models = Path.home() / '.cache' / 'babeldoc' / 'models'",
    "print('models=' + str(len(list(models.glob('*.onnx'))) if models.is_dir() else 0))",
  ].join("\n");

  var CHECK_SMOKE_SCRIPT = [
    "import pymupdf",
    "doc = pymupdf.open()",
    "page = doc.new_page(width=200, height=100)",
    "page.insert_text((20, 50), 'environment check', fontsize=10)",
    "assert 'environment check' in page.get_text()",
    "print('smoke=ok')",
  ].join("\n");

  function checkEnvironment(options) {
    options = options || {};
    if (environmentCheck) return environmentCheck;
    var signal = options.signal;
    environmentCheck = (async function () {
      var checks = [];
      function record(id, label, status, detail) {
        checks.push({ id: id, label: label, status: status, detail: String(detail || "") });
      }
      function throwIfCancelled() {
        if (signal && signal.aborted) throw new Error("环境检测已取消。");
      }
      var base = "";
      try { base = baseDir(); } catch (error) { base = ""; }
      var paths = base ? pathsFor(base) : null;
      var installed = false;
      throwIfCancelled();
      if (paths && fileExists(paths.python)) {
        try {
          var recordJSON = JSON.parse(await IOUtils.readUTF8(paths.installJSON));
          installed = recordJSON.uv === UV_VERSION &&
            recordJSON.python === PYTHON_VERSION &&
            recordJSON.babeldoc === BABELDOC_VERSION;
          record("install", "安装记录", installed ? "pass" : "fail",
                 installed ? "uv、Python、BabelDOC 版本与当前插件一致"
                           : "安装记录版本与当前插件不一致；请点击“修复环境”");
        } catch (error) {
          record("install", "安装记录", "fail", "缺少或无法读取安装记录；请点击“修复环境”");
        }
      } else {
        record("install", "翻译环境", "fail", "尚未安装；请点击“修复环境”");
      }
      if (installed && paths) {
        var logState = { parts: [] };
        var source = normalizeSource(options.source);
        var probe = await probeChild(paths.python, ["-c", CHECK_PYTHON_SCRIPT], paths,
                                     source, signal, logState);
        throwIfCancelled();
        var values = {};
        String(probe.stdout || "").split(/\r?\n/).forEach(function (line) {
          var index = line.indexOf("=");
          if (index > 0) values[line.slice(0, index).trim()] = line.slice(index + 1).trim();
        });
        record("python", "Python", values.python === PYTHON_VERSION ? "pass" : "fail",
               values.python ? "Python " + values.python : "无法运行翻译环境的 Python");
        var babeldocOK = values.babeldoc === BABELDOC_VERSION;
        record("babeldoc", "BabelDOC", babeldocOK ? "pass" : "fail",
               values.babeldoc ? "BabelDOC " + values.babeldoc : "未安装 BabelDOC");
        var bitstringMajor = parseInt(String(values.bitstring || "").split(".")[0], 10);
        var bitstringOK = Number.isFinite(bitstringMajor) && bitstringMajor < 5;
        record("bitstring", "依赖版本", bitstringOK ? "pass" : "fail",
               values.bitstring ? "bitstring " + values.bitstring : "无法读取 bitstring 版本");
        var fontCount = parseInt(values.fonts, 10);
        record("fonts", "字体资源", fontCount > 0 ? "pass" : "fail",
               fontCount > 0 ? fontCount + " 个字体文件"
                             : "未找到字体资源；请点击“修复环境”重新安装");
        var modelCount = parseInt(values.models, 10);
        record("models", "模型资源", modelCount > 0 ? "pass" : "fail",
               modelCount > 0 ? modelCount + " 个模型文件"
                              : "未找到版面模型；请点击“修复环境”重新安装");
        if (babeldocOK) {
          var smoke = await probeChild(paths.python, ["-c", CHECK_SMOKE_SCRIPT], paths,
                                       source, signal, logState);
          throwIfCancelled();
          record("smoke", "本机处理检查", smoke.exitCode === 0 ? "pass" : "fail",
                 smoke.exitCode === 0 ? "PDF 生成与文字提取正常"
                                      : "小型 PDF 处理检查失败");
        }
      }
      var ok = checks.every(function (item) { return item.status === "pass"; });
      return { ok: ok, checkedAt: new Date().toISOString(), checks: checks };
    })().finally(function () {
      environmentCheck = null;
    });
    return environmentCheck;
  }

  function ensureDirectory(path) {
    return Zotero.File.createDirectoryIfMissingAsync(path);
  }

  function appendLog(logState, title, value) {
    var text = redact(value).replace(/\r/g, "");
    logState.parts.push("\n--- " + title + " ---\n" + text + (text && !text.endsWith("\n") ? "\n" : ""));
  }

  function emitProgress(observer, index, percent, detail) {
    var normalized = Math.max(0, Math.min(100, Number(percent) || 0));
    var progress = {
      step: STEP_NAMES[index - 1],
      stepId: STEP_IDS[index - 1],
      stepIndex: index,
      stepCount: 5,
      stepProgress: normalized,
      overall: Math.round(((index - 1 + normalized / 100) / 5) * 100),
      stage: "准备翻译环境（第 " + index + "/5 步）：" + STEP_NAMES[index - 1],
    };
    if (detail) progress.detail = detail;
    (observer || []).slice().forEach(function (callback) {
      try { callback(progress); } catch (error) { /* UI progress must not block installation. */ }
    });
  }

  function notifyInstallState() {
    stateObservers.slice().forEach(function (callback) {
      try { callback(activeInstall); } catch (error) { /* Settings status must not block installation. */ }
    });
  }

  function shortError(error, stderr) {
    var candidates = String(stderr || "").split(/\r?\n/).map(function (line) {
      return redact(line).trim();
    }).filter(function (line) {
      return line && !/^INFO\b/i.test(line) && !/^warning:/i.test(line);
    });
    var detail = candidates.length
      ? candidates[candidates.length - 1]
      : redact(error && error.message ? error.message : error || "未知错误").trim();
    if (ZoteroTranslateFilesConfig && typeof ZoteroTranslateFilesConfig.formatHTMLDiagnostic === "function") {
      detail = ZoteroTranslateFilesConfig.formatHTMLDiagnostic(detail);
    }
    return redact(detail).slice(-600);
  }

  async function writeLog(logState) {
    if (!logState.path) return;
    try {
      await IOUtils.writeUTF8(logState.path, redact(logState.parts.join("")));
    } catch (error) {
      // Keep the primary install error if a diagnostic write fails.
    }
  }

  function throwIfAborted(signal) {
    if (signal && signal.aborted) throw new Error("BabelDOC 环境安装已取消。");
  }

  async function runChild(command, args, paths, source, signal, logState, options) {
    options = options || {};
    throwIfAborted(signal);
    var Subprocess = ChromeUtils.importESModule("resource://gre/modules/Subprocess.sys.mjs").Subprocess;
    var process;
    try {
      process = await Subprocess.call({
        command: command,
        arguments: args,
        environment: childEnvironment(paths.base, source),
        environmentAppend: true,
        stdout: "pipe",
        stderr: "pipe",
      });
    } catch (error) {
      appendLog(logState, "启动进程 " + command, error && error.message ? error.message : error);
      if (signal && signal.aborted) throw new Error("BabelDOC 环境安装已取消。");
      throw error;
    }

    var abortListener = null;
    if (signal) {
      abortListener = function () {
        try { process.kill(); } catch (error) { /* The child may have exited already. */ }
      };
      signal.addEventListener("abort", abortListener, { once: true });
      if (signal.aborted) abortListener();
    }

    var stdout = "";
    var stderr = "";
    var decoderOut = new TextDecoder("utf-8");
    var decoderErr = new TextDecoder("utf-8");
    async function drain(stream, decoder, assign) {
      while (true) {
        var chunk = await stream.read();
        if (!chunk || !chunk.byteLength) break;
        assign(decoder.decode(chunk, { stream: true }));
      }
      assign(decoder.decode());
    }
    function addOut(chunk) { stdout += chunk; }
    function addErr(chunk) { stderr += chunk; }

    try {
      if (process.stdin && typeof process.stdin.close === "function") process.stdin.close();
      var exitResult = null;
      await Promise.all([
        drain(process.stdout, decoderOut, addOut),
        drain(process.stderr, decoderErr, addErr),
        process.wait().then(function (result) { exitResult = result; }),
      ]);
      appendLog(logState, "命令 " + command + " " + args.join(" ") + " 标准输出", stdout);
      appendLog(logState, "命令 " + command + " " + args.join(" ") + " 标准错误", stderr);
      if (signal && signal.aborted) throw new Error("BabelDOC 环境安装已取消。");
      var exitCode = exitResult && exitResult.exitCode;
      if (options.checkExit !== false && exitCode !== 0) {
        var failure = new Error(shortError(null, stderr) || "进程退出代码：" + exitCode);
        failure.stderr = stderr;
        failure.exitCode = exitCode;
        throw failure;
      }
      return { exitCode: exitCode, stdout: stdout, stderr: stderr };
    } catch (error) {
      if (!error.stderr) error.stderr = stderr;
      appendLog(logState, "命令执行错误", error && error.message ? error.message : error);
      if (signal && signal.aborted) throw new Error("BabelDOC 环境安装已取消。");
      throw error;
    } finally {
      if (signal && abortListener) signal.removeEventListener("abort", abortListener);
    }
  }

  async function probeChild(command, args, paths, source, signal, logState) {
    try {
      return await runChild(command, args, paths, source, signal, logState, { checkExit: false });
    } catch (error) {
      if (signal && signal.aborted) throw error;
      return { exitCode: 1, stdout: "", stderr: error && error.stderr || error && error.message || "" };
    }
  }

  async function hashFile(path) {
    var bytes = await IOUtils.read(path);
    var digest = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest)).map(function (byte) {
      return byte.toString(16).padStart(2, "0");
    }).join("");
  }

  async function downloadWheel(paths, source, architecture, signal, logState, observers) {
    var wheel = selectWheel(architecture);
    var wheelPath = join(paths.cache, "uv-" + UV_VERSION + ".whl");
    var urls = downloadURLs(source, wheel);
    var failures = [];
    for (var index = 0; index < urls.length; index++) {
      throwIfAborted(signal);
      var cancelDownload = null;
      var abortListener = null;
      try {
        if (signal) {
          abortListener = function () { if (cancelDownload) cancelDownload(); };
          signal.addEventListener("abort", abortListener, { once: true });
          if (signal.aborted) abortListener();
        }
        await Zotero.HTTP.download(urls[index], wheelPath, {
          timeout: 60000,
          errorDelayMax: 0,
          noRetryOnThrottle: true,
          cancellerReceiver: function (cancel) {
            cancelDownload = cancel;
            if (signal && signal.aborted) cancelDownload();
          },
          onProgress: function (totalBytes, contentLength) {
            var fraction = contentLength > 0 ? totalBytes / contentLength * 100 : 0;
            emitProgress(observers, 1, Math.min(99, fraction), contentLength
              ? Math.round(totalBytes / 1024) + " / " + Math.round(contentLength / 1024) + " KB"
              : Math.round(totalBytes / 1024) + " KB");
          },
        });
        var actualHash = await hashFile(wheelPath);
        if (actualHash !== wheel.sha256) {
          await IOUtils.remove(wheelPath, { ignoreAbsent: true });
          throw new Error("uv 下载文件 SHA-256 校验失败；期望 " + wheel.sha256 + "，实际 " + actualHash + "。");
        }
        appendLog(logState, "下载 wheel", urls[index] + "\nSHA-256 校验通过：" + actualHash);
        return wheelPath;
      } catch (error) {
        try { await IOUtils.remove(wheelPath, { ignoreAbsent: true }); } catch (removeError) { /* Ignore partial-file cleanup errors. */ }
        if (signal && signal.aborted) throw new Error("BabelDOC 环境安装已取消。");
        if (/SHA-256 校验失败/.test(String(error && error.message))) throw error;
        failures.push(redact(error && error.message ? error.message : error));
        appendLog(logState, "下载失败 " + urls[index], error && error.message ? error.message : error);
      } finally {
        if (signal && abortListener) signal.removeEventListener("abort", abortListener);
      }
    }
    throw new Error("无法下载 uv wheel：" + failures.join("；"));
  }

  async function extractUV(wheelPath, uvPath) {
    await IOUtils.remove(uvPath, { ignoreAbsent: true });
    var zipReader = Cc["@mozilla.org/libjar/zip-reader;1"].createInstance(Ci.nsIZipReader);
    try {
      zipReader.open(Zotero.File.pathToFile(wheelPath));
      var entryName = "uv-" + UV_VERSION + ".data/scripts/uv.exe";
      if (!zipReader.hasEntry(entryName)) throw new Error("uv wheel 中缺少 uv.exe。");
      zipReader.extract(entryName, Zotero.File.pathToFile(uvPath));
    } finally {
      try { zipReader.close(); } catch (error) { /* Preserve extraction outcome. */ }
    }
  }

  function makeLogPath() {
    var timestamp = new Date().toISOString().replace(/[:.]/g, "-");
    return join(Zotero.DataDirectory.dir, "babeldoc-for-zotero", "logs", "install-" + timestamp + ".log");
  }

  function promptRetry(stepName, detail, logPath) {
    var parent = null;
    try { parent = Zotero.getMainWindow(); } catch (error) { parent = null; }
    var title = "BabelDOC 环境安装失败";
    var message = "步骤：" + stepName + "\n错误：" + redact(detail).slice(-600) + "\n日志：" + logPath + "\n\n重试安装，或取消并稍后从翻译任务/设置中恢复。";
    var prompt = Services.prompt;
    var flags = prompt.BUTTON_TITLE_IS_STRING * prompt.BUTTON_POS_0 +
      prompt.BUTTON_TITLE_IS_STRING * prompt.BUTTON_POS_1 + prompt.BUTTON_POS_1_DEFAULT;
    return prompt.confirmEx(parent, title, message, flags, "重试", "取消", null, null, {});
  }

  function hasInteractiveWaiter() {
    return inFlightWaiters.some(function (waiter) { return waiter.interactive; });
  }

  async function executeInstall(options, observers) {
    options = options || {};
    var source = normalizeSource(options.source);
    var logState = { path: makeLogPath(), parts: [] };
    var base;
    var paths;
    var architecture;
    var stepIndex = 0;
    var startAt = 0;
    activeInstall = true;
    notifyInstallState();

    while (true) {
      try {
        await ensureDirectory(join(Zotero.DataDirectory.dir, "babeldoc-for-zotero"));
        await ensureDirectory(join(Zotero.DataDirectory.dir, "babeldoc-for-zotero", "logs"));
        base = baseDir();
        paths = pathsFor(base);
        architecture = Services.sysinfo.getProperty("arch");
        // A stale manifest must not make a partially repaired environment look ready.
        await IOUtils.remove(paths.installJSON, { ignoreAbsent: true });
        await ensureDirectory(paths.base);
        await ensureDirectory(paths.uvDir);
        await ensureDirectory(paths.pythonInstallDir);
        await ensureDirectory(paths.cache);
        break;
      } catch (error) {
        var setupDetail = shortError(error);
        appendLog(logState, "准备安装目录失败", setupDetail);
        await writeLog(logState);
        if (options.signal && options.signal.aborted) {
          var setupCancelled = new Error("BabelDOC 环境安装已取消。请重新翻译或在设置中安装/修复环境以继续。日志：" + logState.path);
          setupCancelled.logPath = logState.path;
          activeInstall = false;
          notifyInstallState();
          throw setupCancelled;
        }
        if (!hasInteractiveWaiter()) {
          var setupFailed = new Error("BabelDOC 环境安装失败（准备安装目录）：" + setupDetail + "。请重新翻译或在 Zotero 设置中点击“安装 / 修复环境”以继续。日志：" + logState.path);
          setupFailed.logPath = logState.path;
          activeInstall = false;
          notifyInstallState();
          await writeLog(logState);
          throw setupFailed;
        }
        var setupChoice;
        try { setupChoice = promptRetry("准备安装目录", setupDetail, logState.path); }
        catch (promptError) { setupChoice = 1; }
        if (setupChoice === 0) continue;
        var setupFailed = new Error("BabelDOC 环境安装失败（准备安装目录）：" + setupDetail + "。请重新翻译或在 Zotero 设置中点击“安装 / 修复环境”以继续。日志：" + logState.path);
        setupFailed.logPath = logState.path;
        activeInstall = false;
        notifyInstallState();
        await writeLog(logState);
        throw setupFailed;
      }
    }

    var retry = true;
    try {
      while (retry) {
        retry = false;
        for (stepIndex = startAt; stepIndex < 5; stepIndex++) {
          throwIfAborted(options.signal);
          var stepNumber = stepIndex + 1;
          var stepName = STEP_NAMES[stepIndex];
          emitProgress(observers, stepNumber, 0);
          try {
            if (stepIndex === 0) {
              var uvReady = false;
              if (fileExists(paths.uv)) {
              var versionResult = await probeChild(paths.uv, ["--version"], paths, source, options.signal, logState);
                uvReady = versionResult.exitCode === 0 && isExpectedUVVersion(versionResult.stdout);
              }
              if (!uvReady) {
                var wheelPath = await downloadWheel(paths, source, architecture, options.signal, logState, observers);
                await extractUV(wheelPath, paths.uv);
                var extractedVersion = await runChild(paths.uv, ["--version"], paths, source, options.signal, logState);
                if (extractedVersion.exitCode !== 0 || !isExpectedUVVersion(extractedVersion.stdout)) {
                  throw new Error("下载的 uv 版本不正确：" + extractedVersion.stdout.trim());
                }
              }
            } else if (stepIndex === 1) {
              var findPython = await probeChild(paths.uv, ["python", "find", PYTHON_VERSION], paths, source, options.signal, logState);
              if (findPython.exitCode !== 0) {
                await runChild(paths.uv, ["python", "install", PYTHON_VERSION], paths, source, options.signal, logState);
              }
            } else if (stepIndex === 2) {
              var envWorks = false;
              if (fileExists(paths.python)) {
                var envCheck = await probeChild(paths.python, ["-c", "import sys; assert sys.version_info[:2] == (3, 12)"], paths, source, options.signal, logState);
                envWorks = envCheck.exitCode === 0;
              }
              if (!envWorks) {
                await IOUtils.remove(paths.environment, { recursive: true, ignoreAbsent: true });
                await runChild(paths.uv, ["venv", paths.environment, "--python", PYTHON_VERSION], paths, source, options.signal, logState);
              }
            } else if (stepIndex === 3) {
              var packageWorks = false;
              if (fileExists(paths.python)) {
                var importCheck = await probeChild(paths.python, ["-c", PACKAGE_PROBE], paths, source, options.signal, logState);
                packageWorks = importCheck.exitCode === 0 && fileExists(paths.babeldoc);
              }
              if (!packageWorks) {
                var installArgs = ["pip", "install", "--reinstall-package", "BabelDOC", "--python", paths.python, "BabelDOC==" + BABELDOC_VERSION, "bitstring<5"];
                if (source === "china") installArgs.push("--index-url", TUNA_INDEX);
                await runChild(paths.uv, installArgs, paths, source, options.signal, logState);
                await runChild(paths.python, ["-c", PACKAGE_PROBE], paths, source, options.signal, logState);
              }
            } else if (stepIndex === 4) {
              await runChild(paths.babeldoc, ["--warmup"], paths, source, options.signal, logState);
            }
            emitProgress(observers, stepNumber, 100);
          } catch (error) {
            var detail = shortError(error, error && error.stderr);
            appendLog(logState, "步骤失败：" + stepName, detail);
            await writeLog(logState);
            if (options.signal && options.signal.aborted) {
              var cancelled = new Error("BabelDOC 环境安装已取消。请重新翻译或在设置中安装/修复环境以继续。日志：" + logState.path);
              cancelled.logPath = logState.path;
              throw cancelled;
            }
            if (!hasInteractiveWaiter()) {
              var nonInteractiveFailure = new Error("BabelDOC 环境安装失败（" + stepName + "）：" + detail + "。请重新翻译或在 Zotero 设置中点击“安装 / 修复环境”以继续。日志：" + logState.path);
              nonInteractiveFailure.logPath = logState.path;
              throw nonInteractiveFailure;
            }
            var choice;
            try { choice = promptRetry(stepName, detail, logState.path); }
            catch (promptError) { choice = 1; }
            if (choice === 0) {
              startAt = stepIndex;
              retry = true;
              break;
            }
            var failed = new Error("BabelDOC 环境安装失败（" + stepName + "）：" + detail + "。请重新翻译或在 Zotero 设置中点击“安装 / 修复环境”以继续。日志：" + logState.path);
            failed.logPath = logState.path;
            throw failed;
          }
        }
        if (!retry) {
          var installRecord = {
            uv: UV_VERSION,
            python: PYTHON_VERSION,
            babeldoc: BABELDOC_VERSION,
            source: source,
            completedAt: new Date().toISOString(),
          };
          await IOUtils.writeUTF8(paths.installJSON, JSON.stringify(installRecord, null, 2));
          await writeLog(logState);
          return paths.python;
        }
      }
    } finally {
      activeInstall = false;
      notifyInstallState();
      progressObservers = [];
      await writeLog(logState);
    }
    return paths.python;
  }

  function removeProgressObserver(observer) {
    var index = progressObservers.indexOf(observer);
    if (index !== -1) progressObservers.splice(index, 1);
  }

  function cleanupWaiter(waiter) {
    if (waiter.signal && waiter.abortListener) {
      waiter.signal.removeEventListener("abort", waiter.abortListener);
      waiter.abortListener = null;
    }
    var index = inFlightWaiters.indexOf(waiter);
    if (index !== -1) inFlightWaiters.splice(index, 1);
  }

  function cleanupInFlight() {
    inFlightWaiters.slice().forEach(function (waiter) {
      if (waiter.signal && waiter.abortListener) {
        waiter.signal.removeEventListener("abort", waiter.abortListener);
        waiter.abortListener = null;
      }
    });
    inFlightWaiters = [];
    inFlightAbortController = null;
    inFlight = null;
    progressObservers = [];
  }

  function makeAbortController() {
    if (typeof AbortController === "function") return new AbortController();
    var listeners = [];
    var signal = {
      aborted: false,
      addEventListener: function (type, listener) {
        if (type === "abort" && typeof listener === "function" && !signal.aborted) listeners.push(listener);
      },
      removeEventListener: function (type, listener) {
        if (type === "abort") listeners = listeners.filter(function (entry) { return entry !== listener; });
      },
    };
    return {
      signal: signal,
      abort: function () {
        if (signal.aborted) return;
        signal.aborted = true;
        var pendingListeners = listeners.slice();
        listeners = [];
        pendingListeners.forEach(function (listener) {
          try {
            listener.call(signal, { type: "abort", target: signal, currentTarget: signal });
          } catch (error) {
            // One listener must not prevent the remaining abort handlers from running.
          }
        });
      },
    };
  }

  function waitForInFlight(options) {
    var observer = typeof options.onProgress === "function" ? options.onProgress : null;
    if (observer) progressObservers.push(observer);

    var waiter = {
      signal: options.signal || null,
      interactive: options.interactive !== false,
      abortListener: null,
    };
    inFlightWaiters.push(waiter);
    if (!waiter.signal) return inFlight;

    var installPromise = inFlight;
    return new Promise(function (resolve, reject) {
      var active = true;
      function finish(settle, value) {
        if (!active) return;
        active = false;
        cleanupWaiter(waiter);
        if (observer) removeProgressObserver(observer);
        settle(value);
      }
      waiter.abortListener = function () {
        finish(reject, new Error("翻译任务已取消。"));
        if (!inFlightWaiters.length && inFlightAbortController && !inFlightAbortController.signal.aborted) {
          inFlightAbortController.abort();
        }
      };
      waiter.signal.addEventListener("abort", waiter.abortListener, { once: true });
      if (waiter.signal.aborted) {
        waiter.abortListener();
        return;
      }
      installPromise.then(function (value) {
        finish(resolve, value);
      }, function (error) {
        finish(reject, error);
      });
    });
  }

  function isLifecycleCurrent(generation) {
    return acceptingInstalls && generation === lifecycleGeneration;
  }

  function inactiveError() {
    return new Error("BabelDOC installer is inactive.");
  }

  function assertRepairActive(generation, signal) {
    if (!isLifecycleCurrent(generation)) throw inactiveError();
    if (signal && signal.aborted) throw new Error("修复环境已取消。");
  }

  function waitForRestart(options, previousInstall, generation) {
    return new Promise(function (resolve, reject) {
      var signal = options.signal || null;
      var active = true;
      function cleanup() {
        if (signal && abortListener) signal.removeEventListener("abort", abortListener);
        abortListener = null;
      }
      function restart() {
        if (!active) return;
        active = false;
        cleanup();
        if (!isLifecycleCurrent(generation)) {
          reject(inactiveError());
          return;
        }
        ensureReady(options, generation).then(resolve, reject);
      }
      function cancel() {
        if (!active) return;
        active = false;
        cleanup();
        reject(new Error("翻译任务已取消。"));
      }
      var abortListener = signal ? cancel : null;
      if (signal) {
        signal.addEventListener("abort", abortListener, { once: true });
        if (signal.aborted) {
          cancel();
          return;
        }
      }
      previousInstall.then(restart, restart);
    });
  }

  function waitForLegacyInstall(options, barrier, generation) {
    return new Promise(function (resolve, reject) {
      var signal = options.signal || null;
      var active = true;
      var waiter = { cancel: cancel };
      function cleanup() {
        if (signal && waiter.abortListener) signal.removeEventListener("abort", waiter.abortListener);
        waiter.abortListener = null;
        var index = legacyInstallWaiters.indexOf(waiter);
        if (index !== -1) legacyInstallWaiters.splice(index, 1);
      }
      function cancel(error) {
        if (!active) return;
        active = false;
        cleanup();
        reject(error);
      }
      function continueAfterLegacyInstall() {
        if (!active) return;
        active = false;
        cleanup();
        if (legacyInstallBarrier === barrier) legacyInstallBarrier = null;
        if (!isLifecycleCurrent(generation)) {
          reject(inactiveError());
          return;
        }
        try {
          Promise.resolve(ensureReady(options, generation)).then(resolve, reject);
        } catch (error) {
          reject(error);
        }
      }

      legacyInstallWaiters.push(waiter);
      if (signal) {
        waiter.abortListener = function () { cancel(new Error("Install waiter was cancelled.")); };
        signal.addEventListener("abort", waiter.abortListener, { once: true });
        if (signal.aborted) {
          waiter.abortListener();
          return;
        }
      }
      barrier.then(continueAfterLegacyInstall, continueAfterLegacyInstall);
    });
  }

  function waitForRepair(options, operation, generation) {
    return new Promise(function (resolve, reject) {
      var signal = options.signal || null;
      var active = true;
      function cleanup() {
        if (signal && abortListener) signal.removeEventListener("abort", abortListener);
        abortListener = null;
      }
      function finish(settle, value) {
        if (!active) return;
        active = false;
        cleanup();
        settle(value);
      }
      function resume(value) {
        if (!active) return;
        if (!isLifecycleCurrent(generation)) {
          finish(reject, inactiveError());
        } else if (value === null) {
          Promise.resolve(ensureReady(options, generation)).then(
            result => finish(resolve, result),
            error => finish(reject, error),
          );
        } else {
          finish(resolve, value);
        }
      }
      var abortListener = signal ? function () {
        finish(reject, new Error("翻译任务已取消。"));
      } : null;
      if (signal) {
        signal.addEventListener("abort", abortListener, { once: true });
        if (signal.aborted) {
          abortListener();
          return;
        }
      }
      operation.then(resume, function (error) { finish(reject, error); });
    });
  }

  function maintenanceQueue() {
    try { return Zotero && Zotero.__babeldocForZoteroQueue; } catch (error) { return null; }
  }

  function repairError(code, message) {
    var error = new Error(message);
    error.code = code;
    return error;
  }

  function repairQueueLock() {
    var queue = maintenanceQueue();
    if (!queue || typeof queue.acquireMaintenanceLock !== "function") {
      throw repairError("REPAIR_QUEUE_UNAVAILABLE", "无法确认翻译队列状态，请重启 Zotero 后重试修复环境。");
    }
    var result = queue.acquireMaintenanceLock();
    if (!result || !result.acquired) {
      if (result && result.reason === "translation") {
        throw repairError("TRANSLATION_RUNNING", "翻译正在运行，无法修复翻译环境。请等待翻译任务完成后重试。");
      }
      throw repairError("ENVIRONMENT_MAINTENANCE_BUSY", "翻译环境正在维护，当前无法开始另一项修复。");
    }
    return result.release;
  }

  async function stopAndWaitForInstalls() {
    var currentInstall = inFlight;
    var currentLegacyBarrier = legacyInstallBarrier;
    if (inFlightAbortController && !inFlightAbortController.signal.aborted) {
      inFlightAbortController.abort();
    }
    if (currentLegacyBarrier && installerPriorSingleton &&
        typeof installerPriorSingleton.cancelInFlight === "function") {
      try { installerPriorSingleton.cancelInFlight(); } catch (error) { /* The barrier still guards deletion. */ }
      try {
        if (!installerPriorSingleton.isInstalling() && typeof installerPriorBarrierFinish === "function") {
          installerPriorBarrierFinish();
        }
      } catch (error) {
        // Keep waiting for the existing handoff observer when state cannot be read.
      }
    }
    var waits = [];
    if (currentInstall) waits.push(Promise.resolve(currentInstall).catch(function () {}));
    if (currentLegacyBarrier) waits.push(Promise.resolve(currentLegacyBarrier).catch(function () {}));
    await Promise.all(waits);
  }

  async function promptRepair() {
    var parent = null;
    try { parent = Zotero.getMainWindow(); } catch (error) { parent = null; }
    var localization = parent && parent.document && parent.document.l10n;
    if (!localization || typeof localization.formatValue !== "function") {
      throw new Error("无法读取翻译环境修复确认框的本地化文本。");
    }
    if (parent.MozXULElement && typeof parent.MozXULElement.insertFTLIfNeeded === "function") {
      parent.MozXULElement.insertFTLIfNeeded("babeldoc-for-zotero.ftl");
    }
    var messageIDs = [
      "babeldoc-for-zotero-pref-environment-repair-confirm-title",
      "babeldoc-for-zotero-pref-environment-repair-confirm-message",
      "babeldoc-for-zotero-pref-environment-repair-confirm",
      "babeldoc-for-zotero-pref-environment-repair-cancel",
    ];
    var strings = await Promise.all(messageIDs.map(function (messageID) {
      return localization.formatValue(messageID);
    }));
    if (strings.some(function (value) { return !value; })) {
      throw new Error("无法读取翻译环境修复确认框的本地化文本。");
    }
    var prompt = Services.prompt;
    var flags = prompt.BUTTON_TITLE_IS_STRING * prompt.BUTTON_POS_0 +
      prompt.BUTTON_TITLE_IS_STRING * prompt.BUTTON_POS_1 + prompt.BUTTON_POS_1_DEFAULT;
    return prompt.confirmEx(
      parent,
      strings[0],
      strings[1],
      flags,
      strings[2],
      strings[3],
      null,
      null,
      {},
    );
  }

  function repairEnvironment(options) {
    options = options || {};
    var generation = lifecycleGeneration;
    if (!isLifecycleCurrent(generation)) return Promise.reject(inactiveError());
    if (options.signal && options.signal.aborted) return Promise.reject(new Error("修复环境已取消。"));
    if (repairOperation) return repairOperation;

    var releaseQueue;
    try { releaseQueue = repairQueueLock(); }
    catch (error) { return Promise.reject(error); }

    var operation;
    operation = (async function () {
      try {
        var readyBeforeStop = await isReady();
        assertRepairActive(generation, options.signal);
        if (readyBeforeStop) {
          var choice;
          try { choice = await promptRepair(); }
          catch (error) { choice = 1; }
          assertRepairActive(generation, options.signal);
          if (choice !== 0) return null;
        }

        await stopAndWaitForInstalls();
        assertRepairActive(generation, options.signal);
        if (!readyBeforeStop) {
          var readyAfterStop = await isReady();
          assertRepairActive(generation, options.signal);
          if (readyAfterStop) {
            var completedInstallChoice;
            try { completedInstallChoice = await promptRepair(); }
            catch (error) { completedInstallChoice = 1; }
            assertRepairActive(generation, options.signal);
            if (completedInstallChoice !== 0) return null;
          }
        }

        assertRepairActive(generation, options.signal);
        if (typeof options.onStart === "function") options.onStart();
        assertRepairActive(generation, options.signal);
        try {
          await removeManagedBase(baseDir());
          repairDeletionError = null;
        } catch (error) {
          if (error && error.code === "ENVIRONMENT_DELETE_PROTECTED") {
            repairDeletionError = error;
            throw error;
          }
          repairDeletionError = repairError("ENVIRONMENT_DELETE_FAILED", "无法删除翻译环境，文件可能正在使用；未开始重新安装。请关闭相关程序后重试。");
          throw repairDeletionError;
        }

        // Start the ordinary installer against the now-empty base directory.
        // Publish its shared in-flight promise before allowing another caller in.
        if (repairOperation === operation) repairOperation = null;
        return await ensureReady(options, generation);
      } finally {
        if (repairOperation === operation) repairOperation = null;
        try { releaseQueue(); } catch (error) { /* Queue cleanup must not mask the repair result. */ }
      }
    })();
    repairOperation = operation;
    return operation;
  }

  function ensureReady(options, expectedGeneration) {
    options = options || {};
    var generation = expectedGeneration == null ? lifecycleGeneration : expectedGeneration;
    if (!isLifecycleCurrent(generation)) return Promise.reject(inactiveError());
    if (options.signal && options.signal.aborted) return Promise.reject(new Error("Install waiter was cancelled."));

    if (repairOperation) return waitForRepair(options, repairOperation, generation);
    if (repairDeletionError) return Promise.reject(repairDeletionError);

    if (legacyInstallBarrier) {
      return waitForLegacyInstall(options, legacyInstallBarrier, generation);
    }

    if (inFlight && inFlightAbortController && inFlightAbortController.signal.aborted) {
      return waitForRestart(options, inFlight, generation);
    }

    if (!inFlight) {
      inFlightAbortController = makeAbortController();
      var installOptions = Object.assign({}, options, { signal: inFlightAbortController.signal });
      inFlight = isReady().then(function (ready) {
        if (!isLifecycleCurrent(generation) || installOptions.signal.aborted) throw inactiveError();
        if (ready) return pathsFor(baseDir()).python;
        return executeInstall(installOptions, progressObservers);
      }).finally(cleanupInFlight);
    }
    return waitForInFlight(options);
  }

  function activate() {
    if (!acceptingInstalls) {
      lifecycleGeneration++;
      acceptingInstalls = true;
    }
  }

  function cancelInFlight() {
    acceptingInstalls = false;
    lifecycleGeneration++;
    legacyInstallWaiters.slice().forEach(function (waiter) { waiter.cancel(inactiveError()); });
    if (inFlightAbortController && !inFlightAbortController.signal.aborted) {
      inFlightAbortController.abort();
    }
  }

    return {
    isReady: isReady,
    ensureReady: ensureReady,
    repairEnvironment: repairEnvironment,
    activate: activate,
    createInstallAbortController: makeAbortController,
    cancelInFlight: cancelInFlight,
    baseDir: baseDir,
    isInstalling: function () { return activeInstall; },
    checkEnvironment: checkEnvironment,
    addStateListener: function (callback) {
      if (typeof callback !== "function") return function () {};
      stateObservers.push(callback);
      callback(activeInstall);
      return function () {
        stateObservers = stateObservers.filter(function (listener) { return listener !== callback; });
      };
    },
    versions: { uv: UV_VERSION, python: PYTHON_VERSION, babeldoc: BABELDOC_VERSION },
    _test: {
      normalizeSource: normalizeSource,
      isExpectedUVVersion: isExpectedUVVersion,
      selectWheel: selectWheel,
      downloadURLs: downloadURLs,
      childEnvironment: childEnvironment,
      packageProbe: PACKAGE_PROBE,
      checkPythonScript: CHECK_PYTHON_SCRIPT,
      checkSmokeScript: CHECK_SMOKE_SCRIPT,
      buildStepPlan: buildStepPlan,
      indexURL: function (source) { return normalizeSource(source) === "china" ? TUNA_INDEX : ""; },
      shortError: shortError,
      hashFile: hashFile,
      pathsFor: pathsFor,
      removeManagedBase: removeManagedBase,
      stepNames: STEP_NAMES.slice(),
      stepIDs: STEP_IDS.slice(),
      constants: { tunaIndex: TUNA_INDEX, pythonMirror: PYTHON_MIRROR },
    },
  };
})(installerPriorBarrier);

ZoteroTranslateFilesInstaller.__moduleVersion = 3;
try {
  Zotero.__babeldocForZoteroInstaller = ZoteroTranslateFilesInstaller;
} catch (error) {
  // Keep the local singleton if the host exposes a non-extensible Zotero object.
}
