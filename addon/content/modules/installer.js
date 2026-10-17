/* global Zotero, ZoteroTranslateFilesConfig, Services, IOUtils, PathUtils, ChromeUtils, Cc, Ci, crypto */

var ZoteroTranslateFilesInstaller = Zotero.__babeldocForZoteroInstaller &&
  Zotero.__babeldocForZoteroInstaller.__moduleVersion === 1
  ? Zotero.__babeldocForZoteroInstaller
  : (function () {
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
  var inFlight = null;
  var inFlightAbortController = null;
  var inFlightWaiters = [];
  var progressObservers = [];
  var stateObservers = [];
  var activeInstall = false;

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

  function buildStepPlan(state, force) {
    state = state || {};
    force = !!force;
    return STEP_NAMES.map(function (name, index) {
      var complete = index === 0 ? !!state.uvReady
        : index === 1 ? !!state.pythonReady
          : !force && index === 2 ? !!state.environmentReady
            : !force && index === 3 ? !!state.babeldocReady
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
          cancellerReceiver: function (cancel) { cancelDownload = cancel; },
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
        if (options.force) {
          await IOUtils.remove(paths.installJSON, { ignoreAbsent: true });
          await IOUtils.remove(paths.environment, { recursive: true, ignoreAbsent: true });
        } else {
          // A stale manifest must not make a partially repaired environment look ready.
          await IOUtils.remove(paths.installJSON, { ignoreAbsent: true });
        }
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
                packageWorks = importCheck.exitCode === 0;
              }
              if (!packageWorks) {
                var installArgs = ["pip", "install", "--python", paths.python, "BabelDOC==" + BABELDOC_VERSION, "bitstring<5"];
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

    var waiter = { signal: options.signal || null, abortListener: null };
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

  function waitForRestart(options, previousInstall) {
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
        ensureReady(options).then(resolve, reject);
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

  function ensureReady(options) {
    options = options || {};
    if (options.signal && options.signal.aborted) return Promise.reject(new Error("翻译任务已取消。"));

    if (inFlight && inFlightAbortController && inFlightAbortController.signal.aborted) {
      return waitForRestart(options, inFlight);
    }

    if (!inFlight) {
      inFlightAbortController = makeAbortController();
      var installOptions = Object.assign({}, options, { signal: inFlightAbortController.signal });
      if (!options.force) {
        inFlight = isReady().then(function (ready) {
          if (ready) return pathsFor(baseDir()).python;
          return executeInstall(installOptions, progressObservers);
        }).finally(cleanupInFlight);
      } else {
        inFlight = executeInstall(installOptions, progressObservers).finally(cleanupInFlight);
      }
    }
    return waitForInFlight(options);
  }

  return {
    isReady: isReady,
    ensureReady: ensureReady,
    baseDir: baseDir,
    isInstalling: function () { return activeInstall; },
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
      buildStepPlan: buildStepPlan,
      indexURL: function (source) { return normalizeSource(source) === "china" ? TUNA_INDEX : ""; },
      shortError: shortError,
      hashFile: hashFile,
      pathsFor: pathsFor,
      stepNames: STEP_NAMES.slice(),
      constants: { tunaIndex: TUNA_INDEX, pythonMirror: PYTHON_MIRROR },
    },
  };
})();

ZoteroTranslateFilesInstaller.__moduleVersion = 1;
try {
  Zotero.__babeldocForZoteroInstaller = ZoteroTranslateFilesInstaller;
} catch (error) {
  // Keep the local singleton if the host exposes a non-extensible Zotero object.
}
