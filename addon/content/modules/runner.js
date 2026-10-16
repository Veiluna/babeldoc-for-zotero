/* global Zotero, ZoteroTranslateFiles, ZoteroTranslateFilesConfig, ChromeUtils, IOUtils, PathUtils */

var ZoteroTranslateFilesRunner = (function () {
  var RUNNER_FILENAME = "ztf_babeldoc_runner.py";

  function redact(value, apiKey) {
    return ZoteroTranslateFilesConfig.redact(value, apiKey);
  }

  function pathToFile(path) {
    return Zotero.File.pathToFile(path);
  }

  function isExistingFile(path) {
    if (!path) return false;
    try {
      var file = pathToFile(path);
      return file.exists() && file.isFile();
    } catch (error) {
      return false;
    }
  }

  function createUniqueSuffix() {
    if (typeof Services !== "undefined" && Services.uuid) {
      return Services.uuid.generateUUID().toString().replace(/[{}]/g, "");
    }
    return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
  }

  function readStream(stream, onChunk) {
    return (async function () {
      if (!stream || typeof stream.read !== "function") {
        throw new Error("BabelDOC 输出流不可读取。");
      }

      var decoder = new TextDecoder("utf-8");
      while (true) {
        var chunk = await stream.read();
        var byteLength = chunk && Number(chunk.byteLength);
        if (!byteLength) break;

        // Read raw bytes so a chunk ending mid-character is not mistaken for EOF.
        var decoded = decoder.decode(chunk, { stream: true });
        if (decoded) onChunk(decoded);
      }

      var remaining = decoder.decode();
      if (remaining) onChunk(remaining);
    })();
  }

  function appendTail(lines, line) {
    lines.push(line);
    if (lines.length > 50) lines.splice(0, lines.length - 50);
  }

  function isValidOutput(path) {
    if (!isExistingFile(path)) return false;
    try {
      var file = pathToFile(path);
      return Number(file.fileSize) > 0;
    } catch (error) {
      return false;
    }
  }

  async function findMonolingualOutput(workDir) {
    var candidate = null;
    await Zotero.File.iterateDirectory(workDir, function (entry) {
      if (!entry.isDir && entry.path && /\.zh\.mono\.pdf$/i.test(entry.name || "")) {
        candidate = entry.path;
      }
    });
    return candidate;
  }

  function formatFailure(exitCode, errorText, stderrLines, streamError, apiKey, reason) {
    var detail = errorText || firstDiagnosticLine(stderrLines) || streamError || "没有收到 BabelDOC 结果文件。";
    if (ZoteroTranslateFilesConfig && typeof ZoteroTranslateFilesConfig.formatHTMLDiagnostic === "function") {
      detail = ZoteroTranslateFilesConfig.formatHTMLDiagnostic(detail);
    }
    detail = redact(detail, apiKey).trim();
    if (detail.length > 5000) detail = detail.slice(-5000);
    var codeText = exitCode === 0 ? "" : "（退出码 " + exitCode + "）";
    var reasonText = reason ? reason + (detail ? "\n" + detail : "") : detail;
    return "BabelDOC 翻译失败" + codeText + (reasonText ? "：\n" + reasonText : "。");
  }

  function firstDiagnosticLine(lines) {
    var candidates = (lines || []).map(function (line) {
      return String(line || "").trim();
    }).filter(Boolean);
    var errorLine = candidates.find(function (line) {
      return /\b(?:ERROR|HTTP\s+\d{3}|Error code:\s*\d{3})\b|API(?:Status|Connection|Error)|rate.?limit|too many requests|quota|额度|次数|限流|ContentFilterError/i.test(line);
    });
    if (errorLine) return errorLine;
    for (var index = candidates.length - 1; index >= 0; index--) {
      if (/^Traceback \(most recent call last\):$/.test(candidates[index])) {
        var tracebackTail = candidates.slice(index + 1).filter(Boolean);
        return tracebackTail[tracebackTail.length - 1] || candidates[index];
      }
    }
    var nonInfoLine = candidates.find(function (line) {
      return !/\bINFO\b|Translation completed\./i.test(line);
    });
    return nonInfoLine || candidates[0] || "";
  }

  async function translate(attachmentItem, options) {
    options = options || {};
    var signal = options.signal;
    var onProgress = typeof options.onProgress === "function" ? options.onProgress : function () {};
    var settings = ZoteroTranslateFilesConfig.getSettings();
    var apiKey = settings.apiKey;
    var workDir = null;
    var abortListener = null;
    var proc = null;

    async function cleanup() {
      if (!workDir) return;
      var path = workDir;
      workDir = null;
      await IOUtils.remove(path, { recursive: true, ignoreAbsent: true });
    }

    try {
      if (signal && signal.aborted) throw new Error("翻译任务已取消。");
      if (!settings.apiKey) throw new Error("请先在 Zotero 设置中填写 API key。");
      if (!settings.baseURL || !settings.model || !settings.babeldocPath) {
        throw new Error("请先填写 API Base URL、模型名和 BabelDOC 路径。");
      }

      var normalizedEndpoint = ZoteroTranslateFilesConfig.chatCompletionsURL(settings.baseURL);
      if (normalizedEndpoint.indexOf("?") !== -1) {
        throw new Error(
          "BabelDOC 不支持包含查询参数的 API 地址。请移除 URL 查询参数后再翻译。"
        );
      }
      var babeldocBaseURL = ZoteroTranslateFilesConfig.openAIBaseURLForBabelDOC(
        normalizedEndpoint
      );

      var inputPath;
      try {
        inputPath = await attachmentItem.getFilePathAsync();
      } catch (error) {
        inputPath = null;
      }
      if (!isExistingFile(inputPath)) {
        throw new Error("PDF 文件不可用：附件可能尚未下载，或链接文件已移动。");
      }

      var pythonPath = ZoteroTranslateFilesConfig.resolvePythonFromBabelDOCPath(
        settings.babeldocPath
      );
      if (!isExistingFile(pythonPath)) {
        throw new Error("找不到 BabelDOC 环境中的 python.exe，请检查设置里的可执行文件路径。");
      }

      var { Subprocess } = ChromeUtils.importESModule("resource://gre/modules/Subprocess.sys.mjs");

      var dataDir = PathUtils.join(
        Zotero.DataDirectory.dir,
        "zotero-translate-files"
      );
      await Zotero.File.createDirectoryIfMissingAsync(dataDir);

      var runnerURI = ZoteroTranslateFiles.rootURI + "content/python/" + RUNNER_FILENAME;
      var runnerContents = await Zotero.File.getContentsFromURL(runnerURI);
      if (!runnerContents) throw new Error("无法从插件包中读取 BabelDOC runner 脚本。");
      var runnerPath = PathUtils.join(dataDir, RUNNER_FILENAME);
      // Refresh the copied runner every job so the script from the installed XPI is used.
      await Zotero.File.putContentsAsync(runnerPath, runnerContents, "utf-8");

      var baseTempDir = PathUtils.tempDir;
      workDir = PathUtils.join(baseTempDir, "zotero-translate-files-" + createUniqueSuffix());
      await IOUtils.makeDirectory(workDir);
      var workingDir = PathUtils.join(workDir, "babeldoc-work");
      await IOUtils.makeDirectory(workingDir);

      var inputCopyPath = PathUtils.join(workDir, "input.pdf");
      await IOUtils.copy(inputPath, inputCopyPath);

      var args = [
        runnerPath,
        "--files", inputCopyPath,
        "--output", workDir,
        "--working-dir", workingDir,
        "--openai",
        "--openai-model", settings.model,
        "--openai-base-url", babeldocBaseURL,
        "--lang-in", "en",
        "--lang-out", "zh",
        "--no-dual",
        "--no-auto-extract-glossary",
        "--disable-same-text-fallback",
        "--custom-system-prompt",
        "Translate the complete document as one unit. Translate technical terms, abbreviations, and named entities consistently across every item. Use the translation chosen at the first occurrence again for later occurrences. Keep abbreviations, formulas, citations, and placeholders unchanged. Return exactly the JSON array format required by the surrounding prompt, with one output item for each input item and no extra text.",
        "--watermark-output-mode", "no_watermark",
        "--report-interval", "0.5",
      ];

      // The API key is supplied only through the child environment, never the OS command line.
      if (args.some(function (argument) { return argument === apiKey; })) {
        throw new Error("安全检查失败：API key 不得出现在进程参数中。");
      }

      proc = await Subprocess.call({
        command: pythonPath,
        arguments: args,
        environment: {
          ZTF_OPENAI_API_KEY: apiKey,
          ZTF_MAX_OUTPUT_TOKENS: String(settings.maxOutputTokens),
          ZTF_MAX_REQUESTS: String(settings.maxRequests),
          PYTHONUTF8: "1",
          PYTHONIOENCODING: "utf-8",
          NO_COLOR: "1",
        },
        environmentAppend: true,
        stdout: "pipe",
        stderr: "pipe",
      });

      if (proc.stdin && typeof proc.stdin.close === "function") proc.stdin.close();
      abortListener = function () {
        try {
          proc.kill();
        } catch (error) {
          // The process may have exited between the abort event and kill().
        }
      };
      if (signal) {
        signal.addEventListener("abort", abortListener, { once: true });
        if (signal.aborted) abortListener();
      }

      var stdoutBuffer = "";
      var stderrBuffer = "";
      var stderrLines = [];
      var stderrStreamError = "";
      var resultPath = null;
      var requestCount = null;
      var eventError = "";
      var eventErrorKind = "";
      var warning = null;

      function handleProtocolLine(rawLine) {
        var line = rawLine.replace(/\r$/, "");
        var marker;
        if (line.indexOf("ZTF_PROGRESS ") === 0) marker = "ZTF_PROGRESS ";
        else if (line.indexOf("ZTF_RESULT ") === 0) marker = "ZTF_RESULT ";
        else if (line.indexOf("ZTF_ERROR ") === 0) marker = "ZTF_ERROR ";
        else if (line.indexOf("ZTF_WARNING ") === 0) marker = "ZTF_WARNING ";
        else return;

        var event;
        try {
          event = JSON.parse(line.slice(marker.length));
        } catch (error) {
          return;
        }

        if (marker === "ZTF_PROGRESS ") {
          var overall = Number(event.overall);
          if (!Number.isFinite(overall)) overall = 0;
          var progress = {
            overall: Math.max(0, Math.min(100, overall)),
            stage: String(event.stage || "正在翻译"),
            current: event.current == null ? null : Number(event.current),
            total: event.total == null ? null : Number(event.total),
          };
          if (event.requests != null) {
            var requests = Number(event.requests);
            if (Number.isFinite(requests)) progress.requests = Math.max(0, Math.floor(requests));
          }
          if (event.planned != null) {
            var planned = Number(event.planned);
            if (Number.isFinite(planned)) progress.planned = Math.max(0, Math.floor(planned));
          }
          try {
            onProgress(progress);
          } catch (error) {
            // UI updates must not prevent the child process from being drained.
          }
        } else if (marker === "ZTF_RESULT ") {
          if (event.mono) resultPath = String(event.mono);
          var resultRequests = event.requests == null ? NaN : Number(event.requests);
          if (Number.isFinite(resultRequests)) requestCount = Math.max(0, Math.floor(resultRequests));
        } else if (marker === "ZTF_ERROR ") {
          var errorDetail = event.message || event.error || "BabelDOC 报告了错误。";
          if (ZoteroTranslateFilesConfig && typeof ZoteroTranslateFilesConfig.formatHTMLDiagnostic === "function") {
            errorDetail = ZoteroTranslateFilesConfig.formatHTMLDiagnostic(errorDetail);
          }
          eventError = redact(errorDetail, apiKey);
          eventErrorKind = String(event.kind || "");
        } else if (marker === "ZTF_WARNING ") {
          var failedParagraphs = Number(event.failed_paragraphs);
          warning = {
            failed_paragraphs: Number.isFinite(failedParagraphs)
              ? Math.max(0, Math.floor(failedParagraphs))
              : 0,
            message: redact(event.message || "部分段落翻译失败。", apiKey),
          };
        }
      }

      var stdoutTask = readStream(proc.stdout, function (chunk) {
        stdoutBuffer += chunk;
        var newline;
        while ((newline = stdoutBuffer.indexOf("\n")) !== -1) {
          handleProtocolLine(stdoutBuffer.slice(0, newline));
          stdoutBuffer = stdoutBuffer.slice(newline + 1);
        }
      }).catch(function (error) {
        stderrStreamError = stderrStreamError || redact(error && error.message, apiKey);
      });

      var stderrTask = readStream(proc.stderr, function (chunk) {
        stderrBuffer += chunk;
        var newline;
        while ((newline = stderrBuffer.indexOf("\n")) !== -1) {
          appendTail(stderrLines, redact(stderrBuffer.slice(0, newline).replace(/\r$/, ""), apiKey));
          stderrBuffer = stderrBuffer.slice(newline + 1);
        }
      }).catch(function (error) {
        stderrStreamError = stderrStreamError || redact(error && error.message, apiKey);
      });

      var exitResult = null;
      var waitTask = proc.wait().then(function (result) {
        exitResult = result;
      }).catch(function (error) {
        stderrStreamError = stderrStreamError || redact(error && error.message, apiKey);
      });

      await Promise.all([stdoutTask, stderrTask, waitTask]);
      if (stdoutBuffer) handleProtocolLine(stdoutBuffer);
      if (stderrBuffer) appendTail(stderrLines, redact(stderrBuffer.replace(/\r$/, ""), apiKey));

      if (signal && signal.aborted) throw new Error("翻译任务已取消。");

      var exitCode = exitResult && exitResult.exitCode;
      if (eventError) {
        var eventFailure = new Error(eventError);
        if (eventErrorKind) eventFailure.kind = eventErrorKind;
        throw eventFailure;
      }
      if (exitCode !== 0) {
        throw new Error(formatFailure(exitCode, eventError, stderrLines, stderrStreamError, apiKey));
      }

      if (!resultPath) resultPath = await findMonolingualOutput(workDir);
      if (!isValidOutput(resultPath)) {
        throw new Error(formatFailure(
          exitCode,
          eventError,
          stderrLines,
          stderrStreamError,
          apiKey,
          "未生成有效的中文 PDF。",
        ));
      }

      return {
        outputPath: resultPath,
        warning: warning,
        requestCount: requestCount,
        cleanup: cleanup,
      };
    } catch (error) {
      try {
        await cleanup();
      } catch (cleanupError) {
        // Preserve the original failure; the queue reports cleanup failures after successful imports.
      }
      var wrappedError = new Error(redact(error && error.message ? error.message : error, apiKey));
      if (error && error.kind) wrappedError.kind = error.kind;
      throw wrappedError;
    } finally {
      if (signal && abortListener) signal.removeEventListener("abort", abortListener);
    }
  }

  return { translate: translate };
})();
