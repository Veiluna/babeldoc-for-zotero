/* global Zotero, ZoteroTranslateFiles, ZoteroTranslateFilesConfig, ZoteroTranslateFilesInstaller, ChromeUtils, IOUtils, PathUtils */

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

  async function findDualOutput(workDir) {
    var candidate = null;
    await Zotero.File.iterateDirectory(workDir, function (entry) {
      if (!entry.isDir && entry.path && /\.zh\.dual\.pdf$/i.test(entry.name || "")) {
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
    var outputMode = options.outputMode === "dual" ? "dual" : "mono";
    var settings = ZoteroTranslateFilesConfig.getSettings();
    var apiKey = settings.apiKey;
    var workDir = null;
    var logPath = null;
    var repairRequestCount = null;
    var fragmentItemCount = null;
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
      if (!settings.baseURL || !settings.model) {
        throw new Error("请先填写 API Base URL 和模型名。");
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

      var pythonPath = await ZoteroTranslateFilesInstaller.ensureReady({
        signal: signal,
        source: settings.downloadSource,
        onProgress: function (progress) {
          try { onProgress(progress); } catch (error) { /* Progress UI must not block setup. */ }
        },
      });
      if (!isExistingFile(pythonPath)) {
        throw new Error("无法找到自动安装的 BabelDOC Python 环境，请在设置中点击“修复环境”。");
      }

      var { Subprocess } = ChromeUtils.importESModule("resource://gre/modules/Subprocess.sys.mjs");

      var dataDir = PathUtils.join(
        Zotero.DataDirectory.dir,
        "babeldoc-for-zotero"
      );
      await Zotero.File.createDirectoryIfMissingAsync(dataDir);
      var logDir = PathUtils.join(dataDir, "logs");
      await Zotero.File.createDirectoryIfMissingAsync(logDir);
      var attachmentKey = String(attachmentItem.key || attachmentItem.id || "attachment")
        .replace(/[^A-Za-z0-9_.-]/g, "-");
      var timestamp = new Date().toISOString().replace(/[:.]/g, "-");
      logPath = PathUtils.join(logDir, timestamp + "-" + attachmentKey + ".jsonl");

      var runnerURI = ZoteroTranslateFiles.rootURI + "content/python/" + RUNNER_FILENAME;
      var runnerContents = await Zotero.File.getContentsFromURL(runnerURI);
      if (!runnerContents) throw new Error("无法从插件包中读取 BabelDOC runner 脚本。");
      var runnerPath = PathUtils.join(dataDir, RUNNER_FILENAME);
      // Refresh the copied runner every job so the script from the installed XPI is used.
      await Zotero.File.putContentsAsync(runnerPath, runnerContents, "utf-8");
      // Every Python helper must be copied next to the runner: the runner loads
      // them by path and never executes from inside the addon directory.
      var pythonHelpers = [
        ["ztf_inline_formulas.py", "公式辅助模块"],
        ["ztf_style_guards.py", "样式保护辅助模块"],
        ["ztf_links.py", "链接恢复辅助模块"],
        ["ztf_table_headers.py", "表头识别辅助模块"],
      ];
      for (var helperIndex = 0; helperIndex < pythonHelpers.length; helperIndex++) {
        var helperName = pythonHelpers[helperIndex][0];
        var helperLabel = pythonHelpers[helperIndex][1];
        var helperURI = ZoteroTranslateFiles.rootURI + "content/python/" + helperName;
        var helperContents = await Zotero.File.getContentsFromURL(helperURI);
        if (!helperContents) throw new Error("无法从插件包中读取" + helperLabel + "。");
        await Zotero.File.putContentsAsync(
          PathUtils.join(dataDir, helperName), helperContents, "utf-8"
        );
      }

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
        "--no-auto-extract-glossary",
        "--disable-same-text-fallback",
        // The translation prompt (terminology consistency, fragment rules) is
        // owned by ztf_babeldoc_runner.py so it cannot drift out of sync here.
        "--watermark-output-mode", "no_watermark",
        "--report-interval", "0.5",
      ];

      // mono: no dual PDF; dual: BabelDOC writes the side-by-side dual.
      if (outputMode === "mono") args.push("--no-dual");

      // The API key is supplied only through the child environment, never the OS command line.
      if (args.some(function (argument) { return argument === apiKey; })) {
        throw new Error("安全检查失败：API key 不得出现在进程参数中。");
      }

      proc = await Subprocess.call({
        command: pythonPath,
        arguments: args,
        environment: {
          ZTF_OPENAI_API_KEY: apiKey,
          ZTF_TRANSLATE_REFERENCES: settings.translateReferences ? "1" : "0",
          ZTF_TOKEN_PARAM: settings.tokenParam || "max_tokens",
          ZTF_JOB_LOG_PATH: logPath,
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
      var resultDualPath = null;
      var requestCount = null;
      var eventError = "";
      var eventErrorKind = "";
      var warning = null;
      var linkStats = null;

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
          if (event.dual) resultDualPath = String(event.dual);
          var resultRequests = event.requests == null ? NaN : Number(event.requests);
          if (Number.isFinite(resultRequests)) requestCount = Math.max(0, Math.floor(resultRequests));
          var repairRequests = event.repair_request_count == null ? NaN : Number(event.repair_request_count);
          if (Number.isFinite(repairRequests)) repairRequestCount = Math.max(0, Math.floor(repairRequests));
          var fragmentItems = event.fragment_item_count == null ? NaN : Number(event.fragment_item_count);
          if (Number.isFinite(fragmentItems)) fragmentItemCount = Math.max(0, Math.floor(fragmentItems));
          if (event.log_path) logPath = String(event.log_path);
          if (event.links && typeof event.links === "object") {
            linkStats = {
              source: Number(event.links.source) || 0,
              restored: Number(event.links.restored) || 0,
              unresolved: Number(event.links.unresolved) || 0,
              unsupported: Number(event.links.unsupported) || 0,
              mappingPath: event.links.mapping_path ? String(event.links.mapping_path) : null,
            };
          }
          // The runner adapts if the provider rejects the token-limit parameter;
          // remember what worked so the next job uses it from the first request.
          if (event.token_param && event.token_param !== settings.tokenParam &&
              ZoteroTranslateFilesConfig && typeof ZoteroTranslateFilesConfig.setTokenParam === "function") {
            try {
              ZoteroTranslateFilesConfig.setTokenParam(String(event.token_param));
            } catch (error) {
              // Remembering the dialect is an optimisation; never fail the job for it.
            }
          }
        } else if (marker === "ZTF_ERROR ") {
          var errorDetail = event.message || event.error || "BabelDOC 报告了错误。";
          if (ZoteroTranslateFilesConfig && typeof ZoteroTranslateFilesConfig.formatHTMLDiagnostic === "function") {
            errorDetail = ZoteroTranslateFilesConfig.formatHTMLDiagnostic(errorDetail);
          }
          eventError = redact(errorDetail, apiKey);
          eventErrorKind = String(event.kind || "");
        } else if (marker === "ZTF_WARNING ") {
          var failedParagraphs = Number(event.failed_paragraphs);
          var warningTables = [];
          if (Array.isArray(event.tables)) {
            event.tables.forEach(function (table) {
              if (!table || typeof table !== "object") return;
              var page = Number(table.page);
              warningTables.push({
                table: String(table.table || "未编号表"),
                page: Number.isFinite(page) ? Math.max(1, Math.floor(page)) : null,
                reason: String(table.reason || ""),
              });
            });
          }
          var translatedParagraphs = Number(event.translated_paragraphs);
          warning = {
            failed_paragraphs: Number.isFinite(failedParagraphs)
              ? Math.max(0, Math.floor(failedParagraphs))
              : 0,
            translated_paragraphs: Number.isFinite(translatedParagraphs)
              ? Math.max(0, Math.floor(translatedParagraphs))
              : 0,
            degraded: event.degraded === true,
            message: redact(event.message || "部分段落翻译失败。", apiKey),
            tables: warningTables,
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

      // 请求的输出模式缺失时，只要 BabelDOC 产出了另一种有效 PDF，
      // 就降级导入已有产物并警告，而不是丢掉整篇结果。
      function withDegradedWarning(currentWarning, message) {
        var base = currentWarning || { failed_paragraphs: 0, message: "", tables: [] };
        var mergedMessage = base.message ? base.message + "；" + message : message;
        return {
          failed_paragraphs: base.failed_paragraphs,
          translated_paragraphs: base.translated_paragraphs || 0,
          degraded: true,
          message: redact(mergedMessage, apiKey),
          tables: base.tables || [],
        };
      }

      var artifacts = [];
      if (outputMode === "dual") {
        if (!resultDualPath) resultDualPath = await findDualOutput(workDir);
        if (!resultPath) resultPath = await findMonolingualOutput(workDir);
        if (isValidOutput(resultDualPath)) {
          artifacts = [{ kind: "dual", path: resultDualPath }];
        } else if (isValidOutput(resultPath)) {
          artifacts = [{ kind: "mono", path: resultPath }];
          warning = withDegradedWarning(
            warning,
            "未生成有效的双语对照 PDF，已自动降级导出单语中文译本。",
          );
        } else {
          throw new Error(formatFailure(
            exitCode,
            eventError,
            stderrLines,
            stderrStreamError,
            apiKey,
            "未生成有效的双语对照 PDF。",
          ));
        }
      } else {
        if (!resultPath) resultPath = await findMonolingualOutput(workDir);
        if (isValidOutput(resultPath)) {
          artifacts = [{ kind: "mono", path: resultPath }];
        } else if (isValidOutput(resultDualPath)) {
          // mono 模式传了 --no-dual，正常不会出现双语产物；这是防御
          // BabelDOC 异常写盘顺序的兜底，避免丢弃已生成的有效 PDF。
          artifacts = [{ kind: "dual", path: resultDualPath }];
          warning = withDegradedWarning(
            warning,
            "未生成有效的中文 PDF，已自动降级导出双语对照译本。",
          );
        } else {
          throw new Error(formatFailure(
            exitCode,
            eventError,
            stderrLines,
            stderrStreamError,
            apiKey,
            "未生成有效的中文 PDF。",
          ));
        }
      }

      return {
        outputPath: artifacts[0].path,
        outputPaths: artifacts,
        warning: warning,
        requestCount: requestCount,
        repairRequestCount: repairRequestCount,
        fragmentItemCount: fragmentItemCount,
        logPath: logPath,
        linkStats: linkStats,
        linkMappingPath: linkStats && linkStats.mappingPath ? linkStats.mappingPath : null,
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
      if (error && error.logPath) wrappedError.logPath = error.logPath;
      else if (logPath) wrappedError.logPath = logPath;
      throw wrappedError;
    } finally {
      if (signal && abortListener) signal.removeEventListener("abort", abortListener);
    }
  }

  return { translate: translate };
})();
