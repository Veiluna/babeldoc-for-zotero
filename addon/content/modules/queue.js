/* global Zotero, Services, ZoteroTranslateFiles, ZoteroTranslateFilesAttachments, ZoteroTranslateFilesConfig, ZoteroTranslateFilesRunner */

var ZoteroTranslateFilesQueue = (function () {
  var pending = [];
  var queuedIDs = new Set();
  var runner = ZoteroTranslateFilesRunner;
  var importer = null;
  var currentJob = null;
  var currentController = null;
  var currentProgress = null;
  var stopped = true;
  var idleWaiters = [];
  var maintenanceLocked = false;

  function log(message) {
    if (ZoteroTranslateFiles && typeof ZoteroTranslateFiles.log === "function") {
      ZoteroTranslateFiles.log("queue: " + message);
    } else if (Zotero && typeof Zotero.debug === "function") {
      Zotero.debug("BabelDOC for Zotero: queue: " + message);
    }
  }

  function getAttachmentTitle(item, job) {
    var title = "";
    try {
      if (item && typeof item.getField === "function") {
        title = String(item.getField("title") || "").trim();
      }
    } catch (error) {
      // Use the title captured when the job entered the queue.
    }
    title = title || String(job && job.attachmentTitle || "").trim();
    title = title.replace(/[\r\n\t]+/g, " ");
    return ZoteroTranslateFilesConfig.redact(title || "PDF 附件 #" + (job && job.attachmentID || "?"));
  }

  function getFailureText(error) {
    var text = "";
    if (error && error.message != null) text = String(error.message);
    else text = String(error == null ? "翻译失败" : error);

    var response = error && error.response;
    var responseBody = error && (
      error.responseText
      || error.body
      || (response && (response.responseText || response.body || response.text))
    );
    if (responseBody != null && /<!doctype\s+html|<html\b|<script\b|<title\b/i.test(String(responseBody))) {
      text += "\n" + String(responseBody);
    }

    var status = error && (
      error.status
      || error.statusCode
      || error.status_code
      || (error.response && (error.response.status || error.response.status_code))
    );
    if (status && !/\bHTTP\s+\d{3}\b/i.test(text)) text = "HTTP " + status + ": " + text;
    if (error && error.code && text.indexOf(String(error.code)) < 0) {
      text += "\n错误代码：" + String(error.code);
    }
    return {
      raw: text,
      redacted: ZoteroTranslateFilesConfig.redact(text),
      kind: error && (error.kind || error.error_kind) || "",
    };
  }

  function getFailureCause(text, kind) {
    if (kind === "babeldoc_version") {
      return "自动安装的 BabelDOC 环境版本不正确，请在设置中点击“安装 / 修复环境”";
    }
    if (kind === "request_cap" || /request_cap|单篇请求上限/i.test(text)) {
      return "已达到单篇请求次数上限，任务已停止";
    }
    if (/必须是大于或等于\s*\d+\s*的整数|(?:设置|配置).{0,12}(?:无效|错误)|invalid (?:setting|configuration)/i.test(text)) {
      return "翻译设置无效，请检查各项设置的格式";
    }
    if (/\b(?:401|403)\b|unauthorized|forbidden|invalid[_ -]?api[_ -]?key|incorrect api key|insufficient permission|no permission/i.test(text)) {
      return "API key 无效或无权限";
    }
    if (/\b404\b|model[_ -]+not[_ -]+found|(?:HTTP|endpoint|interface|接口|model|模型).{0,40}(?:not found|does not exist)/i.test(text)) {
      return "接口地址或模型名不正确";
    }
    if (/\b429\b|rate.?limit|too many requests|quota|额度|请求次数(?:上限|已达|达到)|(?:API|接口).{0,20}(?:请求)?次数.{0,10}(?:上限|已达|达到)|限流/i.test(text)) {
      return "请求过于频繁或额度不足（接口可能已达到请求次数上限）";
    }
    if (/<!doctype\s+html|<html\b|<script\b|<title\b/i.test(text)) {
      return "接口返回了网页而不是 API 响应（可能已达到请求次数上限或被拦截）";
    }
    if (/BabelDOC.{0,40}(?:查询参数|query parameters?)/i.test(text)) {
      return "BabelDOC 不支持包含查询参数的接口地址";
    }
    if (/PDF.{0,50}(?:不存在|不可用|未下载|尚未下载|not found|missing)|附件.{0,50}(?:不存在|无法读取|not found|missing)/i.test(text)) {
      return "PDF 文件不存在（可能尚未下载）";
    }
    if (/找不到 BabelDOC|(?:python|babeldoc)(?:\.exe)?[^\r\n]{0,80}(?:not found|missing|does not exist|cannot find|未找到|找不到|不存在)|(?:ENOENT|NS_ERROR_FILE_NOT_FOUND|WinError 2)|No module named ['\"]?babeldoc|ModuleNotFoundError[^\r\n]*babeldoc/i.test(text)) {
      return "找不到 BabelDOC，请重新翻译或在设置中点击“安装 / 修复环境”";
    }
    if (/ECONN(?:REFUSED|RESET|ABORTED)|ENOTFOUND|EAI_AGAIN|ETIMEDOUT|ERR_NETWORK|APIConnectionError|ConnectionError|ConnectError|connection.{0,30}(?:refused|reset|failed|error|timed out)|failed to connect|could not connect|network is unreachable|无法连接|连接失败/i.test(text)) {
      return "无法连接到接口地址";
    }
    if (/未生成有效的中文 PDF|没有收到 BabelDOC 结果文件|没有返回可导入的译本文件|no output|output file.{0,30}(?:missing|not found)/i.test(text)) {
      return "BabelDOC 未生成可导入的中文 PDF";
    }
    if (/导入|译本附件|创建译本|无法生成译本|\bimport(?:er)?\b/i.test(text)) {
      return "无法导入中文译本";
    }
    if (/API Base URL|API key|模型名|Zotero 设置|settings?.{0,30}(?:missing|incomplete)/i.test(text)) {
      return "翻译设置不完整，请检查接口地址、API key 和模型名";
    }
    return "翻译过程中发生错误，请查看技术详情";
  }

  function getTechnicalDetail(text) {
    var original = String(text || "");
    var counts = original.match(/Translation completed\.\s*Total:\s*(\d+)\s*,\s*Successful:\s*(\d+)\s*,\s*Fallback:\s*(\d+)/i);
    var countSummary = counts
      ? counts[1] + " 段中 " + counts[2] + " 段成功（" + counts[3] + " 段保留原文）"
      : "";
    var formatted = typeof ZoteroTranslateFilesConfig.formatHTMLDiagnostic === "function"
      ? ZoteroTranslateFilesConfig.formatHTMLDiagnostic(original)
      : original;
    formatted = ZoteroTranslateFilesConfig.redact(formatted);
    var lines = String(formatted || "").split(/\r?\n/)
      .map(function (line) { return line.trim(); })
      .filter(Boolean);
    var errorLine = lines.find(function (line) {
      return /\b(?:ERROR|HTTP\s+\d{3}|Error code:\s*\d{3})\b|API(?:Status|Connection|Error)|rate.?limit|too many requests|quota|额度|次数|限流|ContentFilterError/i.test(line);
    });
    var detail = errorLine || lines.find(function (line) {
      return !/\bINFO\b|Translation completed\./i.test(line);
    }) || lines[0] || "未提供错误详情";
    if (countSummary && detail.indexOf(countSummary) < 0) {
      detail += "；" + countSummary;
    }
    if (detail.length > 700) detail = "…" + detail.slice(-699);
    return detail;
  }

  function scheduleFailureAlert(message) {
    // The timer lets runJob's finally handler start the next queued job before a modal can block UI work.
    // One timer is kept per failed job; shutdown suppresses alerts that have not appeared yet.
    setTimeout(function () {
      if (stopped) return;

      var parentWindow = null;
      try {
        if (Zotero && typeof Zotero.getMainWindow === "function") {
          parentWindow = Zotero.getMainWindow();
        }
      } catch (error) {
        // The prompt can still be shown without a parent window.
      }

      try {
        if (typeof Services !== "undefined" && Services.prompt && typeof Services.prompt.alert === "function") {
          Services.prompt.alert(parentWindow, "翻译失败", message);
          return;
        }
      } catch (error) {
        // Fall back to Zotero.alert if the platform prompt is unavailable.
      }
      try {
        if (Zotero && typeof Zotero.alert === "function") {
          Zotero.alert(parentWindow, "翻译失败", message);
          return;
        }
      } catch (error) {
        // Reporting UI failures must not interrupt the queue.
      }
      log("could not show translation failure alert");
    }, 0);
  }

  function failureMessage(job, error) {
    var failureText = getFailureText(error);
    var title = getAttachmentTitle(null, job);
    var cause = getFailureCause(failureText.raw, failureText.kind);
    var detail = getTechnicalDetail(failureText.redacted);
    var message = title + "\n原因：" + cause + "\n技术详情：" + detail;
    return {
      // Dynamic title and error detail are already redacted; do not redact the fixed cause text.
      message: message,
      logMessage: "translation failed for attachment " + job.attachmentID + " (" + title + "): " + cause + "; " + detail,
    };
  }

  async function formatLogPath(path) {
    var fallback = "Diagnostic log: " + String(path || "");
    if (!path) return "";
    try {
      var mainWindow = typeof Zotero.getMainWindow === "function"
        ? Zotero.getMainWindow()
        : null;
      var document = mainWindow && !mainWindow.closed && mainWindow.document;
      if (document && document.l10n && typeof document.l10n.formatValue === "function") {
        if (mainWindow.MozXULElement &&
            typeof mainWindow.MozXULElement.insertFTLIfNeeded === "function") {
          mainWindow.MozXULElement.insertFTLIfNeeded("babeldoc-for-zotero.ftl");
        }
        return await document.l10n.formatValue(
          "babeldoc-for-zotero-log-path",
          { path: String(path) },
        ) || fallback;
      }
    } catch (error) {
      // Keep the local path useful when localization is unavailable.
    }
    return fallback;
  }

  function showNotice(descriptions) {
    try {
      var progress = new Zotero.ProgressWindow({ closeOnClick: true });
      progress.changeHeadline("BabelDOC for Zotero");
      for (var description of descriptions) progress.addDescription(description);
      progress.show();
      progress.startCloseTimer(4000);
    } catch (error) {
      // Progress UI failures must not copy arbitrary exception text into logs.
      log("could not show queue notice");
    }
  }

  function showEnqueueNotice(addedCount, aheadCount, duplicateCount) {
    var descriptions = [];
    if (addedCount > 0) {
      descriptions.push(
        "已加入翻译队列：" + addedCount + " 篇（前面还有 " + aheadCount + " 篇）"
      );
    } else if (duplicateCount > 0) {
      descriptions.push("所选 PDF 已在翻译队列中，未重复添加。");
    } else {
      descriptions.push("未找到可翻译的 PDF 附件。");
    }

    if (addedCount > 0 && duplicateCount > 0) {
      descriptions.push("已忽略 " + duplicateCount + " 篇重复任务。");
    }
    showNotice(descriptions);
  }

  function makeAbortController() {
    if (typeof AbortController === "function") return new AbortController();

    var listeners = [];
    var signal = {
      aborted: false,
      addEventListener: function (type, listener, options) {
        if (type !== "abort" || typeof listener !== "function" || signal.aborted) return;
        listeners.push({ listener: listener, once: !!(options && options.once) });
      },
      removeEventListener: function (type, listener) {
        if (type !== "abort") return;
        listeners = listeners.filter(function (entry) {
          return entry.listener !== listener;
        });
      },
    };

    return {
      signal: signal,
      abort: function () {
        if (signal.aborted) return;
        signal.aborted = true;
        var event = { type: "abort", target: signal, currentTarget: signal };
        var pendingListeners = listeners.slice();
        for (var entry of pendingListeners) {
          try {
            entry.listener.call(signal, event);
          } catch (error) {
            // Match native dispatch behavior: one listener cannot block the rest.
          }
          if (entry.once) {
            listeners = listeners.filter(function (candidate) {
              return candidate !== entry;
            });
          }
        }
      },
    };
  }

  function getSettingsError() {
    var settings = ZoteroTranslateFilesConfig.getSettings();
    var missing = [];
    if (!settings.baseURL) missing.push("API Base URL");
    if (!settings.apiKey) missing.push("API key");
    if (!settings.model) missing.push("模型名");
    if (!missing.length) return null;

    return new Error(
      "请先打开 Zotero 设置 → BabelDOC for Zotero，填写：" + missing.join("、") + "。"
    );
  }

  function notifyIdle() {
    if (currentJob || pending.length) return;
    var waiters = idleWaiters;
    idleWaiters = [];
    for (var resolve of waiters) resolve();
  }

  function reportFailure(job, error) {
    try {
      api.onJobFailed(job, error);
    } catch (handlerError) {
      log("onJobFailed failed for attachment " + job.attachmentID);
    }
  }

  function progressText(progress, queueRemaining) {
    var stage = String(progress && progress.stage || "正在翻译");
    var overall = Number(progress && progress.overall);
    if (!Number.isFinite(overall)) overall = 0;
    overall = Math.max(0, Math.min(100, overall));
    var requestText = "";
    var requests = Number(progress && progress.requests);
    var planned = Number(progress && progress.planned);
    if (Number.isFinite(requests)) {
      requestText = " · 请求 " + Math.floor(requests);
      if (Number.isFinite(planned) && planned > 0) requestText += "/" + Math.floor(planned);
      else requestText += " 次";
    }
    return stage + " · " + Math.round(overall) + "%" + requestText + "（队列中还有 " + queueRemaining + " 篇）";
  }

  function refreshProgress(job, progress) {
    if (!currentProgress || currentProgress.job !== job || !currentProgress.active) return;
    var entry = currentProgress;
    var nextProgress = progress || entry.lastProgress;
    if (String(nextProgress.stage || "正在翻译") !== String(entry.lastProgress.stage || "正在翻译")) {
      entry.stageStartedAt = Date.now();
    }
    entry.lastProgress = nextProgress;
    if (!entry.itemProgress) return;

    try {
      var overall = Number(entry.lastProgress && entry.lastProgress.overall);
      if (!Number.isFinite(overall)) overall = 0;
      entry.itemProgress.setProgress(Math.max(0, Math.min(100, overall)));
      var text = progressText(entry.lastProgress, pending.length);
      var seconds = Math.max(0, Math.floor((Date.now() - entry.stageStartedAt) / 1000));
      var elapsed = Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
      var fallback = "Stage elapsed: " + elapsed;
      var sequence = ++entry.sequence;
      entry.itemProgress.setText(text + " · " + fallback);
      formatStageElapsed(elapsed, fallback).then(function (label) {
        if (currentProgress !== entry || !entry.active || entry.sequence !== sequence) return;
        try {
          entry.itemProgress.setText(text + " · " + label);
        } catch (error) {
          // A late localization result must not interrupt the queue.
        }
      });
    } catch (error) {
      // Progress UI errors must not interrupt the translation queue.
    }
  }

  async function formatStageElapsed(elapsed, fallback) {
    try {
      var mainWindow = Zotero.getMainWindow();
      var document = mainWindow && !mainWindow.closed && mainWindow.document;
      if (document && document.l10n && typeof document.l10n.formatValue === "function") {
        if (mainWindow.MozXULElement && typeof mainWindow.MozXULElement.insertFTLIfNeeded === "function") {
          mainWindow.MozXULElement.insertFTLIfNeeded("babeldoc-for-zotero.ftl");
        }
        return await document.l10n.formatValue(
          "babeldoc-for-zotero-progress-stage-elapsed", { elapsed: elapsed }
        ) || fallback;
      }
    } catch (error) {
      // Match the existing localization fallback without exposing exception text.
    }
    return fallback;
  }

  function stopProgressTimer(entry) {
    if (!entry) return;
    entry.active = false;
    entry.sequence++;
    if (entry.timer !== null) clearTimeout(entry.timer);
    entry.timer = null;
    if (entry.popupWindow) {
      try {
        entry.popupWindow.removeEventListener("unload", entry.onPopupUnload);
      } catch (error) {
        // The native window may already have been destroyed.
      }
      entry.popupWindow = null;
      entry.onPopupUnload = null;
    }
  }

  function showProgressWindow(entry) {
    // Zotero 10.0.6 keeps the DOM window private, but registers it with its instance
    // synchronously during show(). Capture only this instance and restore the shared API.
    var windowSet = Zotero.ProgressWindowSet;
    var add = windowSet.add;
    windowSet.add = function (popupWindow, instance) {
      var result = add.apply(this, arguments);
      if (instance === entry.window) {
        entry.popupWindow = popupWindow;
        entry.onPopupUnload = function () { stopProgressTimer(entry); };
        popupWindow.addEventListener("unload", entry.onPopupUnload);
      }
      return result;
    };
    try {
      entry.window.show();
    } finally {
      windowSet.add = add;
    }
  }

  function scheduleProgressRefresh(entry) {
    if (stopped || currentProgress !== entry || !entry.active) return;
    entry.timer = setTimeout(function () {
      entry.timer = null;
      if (stopped || currentProgress !== entry || !entry.active) return;
      refreshProgress(entry.job);
      scheduleProgressRefresh(entry);
    }, 1000);
  }

  function openProgressWindow(job, attachmentItem) {
    var title = "PDF";
    try {
      title = String(attachmentItem.getField("title") || title);
    } catch (error) {
      // Keep the generic title if the attachment cannot provide one.
    }

    var entry = { job: job, window: null, itemProgress: null,
      timer: null, active: true, sequence: 0, stageStartedAt: Date.now(), lastProgress: {
      overall: 0,
      stage: "正在准备 BabelDOC",
    } };
    currentProgress = entry;
    try {
      entry.window = new Zotero.ProgressWindow({ closeOnClick: false });
      // Zotero 10.0.6 also calls this public close method when the parent window closes.
      var close = entry.window.close;
      entry.window.close = function () {
        stopProgressTimer(entry);
        return close.apply(this, arguments);
      };
      entry.window.changeHeadline("正在翻译：" + title);
      var itemTypeIconName = "attachment";
      try {
        if (typeof attachmentItem.getItemTypeIconName === "function") {
          itemTypeIconName = attachmentItem.getItemTypeIconName() || itemTypeIconName;
        }
      } catch (error) {
        // Fall back to the standard attachment icon if the item cannot provide one.
      }
      entry.itemProgress = new entry.window.ItemProgress(
        itemTypeIconName,
        title
      );
      entry.itemProgress.setProgress(0);
      refreshProgress(job);
      showProgressWindow(entry);
      scheduleProgressRefresh(entry);
    } catch (error) {
      stopProgressTimer(entry);
      entry.window = null;
      entry.itemProgress = null;
      log("could not show translation progress window");
    }
  }

  function closeProgressWindow(job, succeeded, aborted, warning, completedTitle, requestCount, logPath) {
    if (!currentProgress || currentProgress.job !== job) return;
    var entry = currentProgress;
    stopProgressTimer(entry);
    currentProgress = null;
    if (!entry.window || !entry.itemProgress) return;

    try {
      if (succeeded) {
        entry.itemProgress.setProgress(100);
        var failedParagraphs = Number(warning && warning.failed_paragraphs);
        var completionText = "已完成：" + String(completedTitle || "中文译本");
        if (Number.isFinite(failedParagraphs) && failedParagraphs > 0) {
          completionText += "（有 " + Math.floor(failedParagraphs) + " 段翻译失败，保留原文）";
        }
        var totalRequests = requestCount == null ? NaN : Number(requestCount);
        if (Number.isFinite(totalRequests)) {
          completionText += "（共请求 " + Math.floor(totalRequests) + " 次）";
        }
        entry.itemProgress.setText(completionText);
        if (logPath) {
          formatLogPath(logPath).then(function (label) {
            if (!entry.window.closed && entry.itemProgress) {
              entry.itemProgress.setText(completionText + "\n" + label);
            }
          });
        }
      } else if (aborted) {
        entry.itemProgress.setText("翻译已取消。");
      } else {
        if (typeof entry.itemProgress.setError === "function") entry.itemProgress.setError();
        entry.itemProgress.setText("翻译失败。");
      }
      entry.window.startCloseTimer(5000);
    } catch (error) {
      // A closing progress window must not affect import or queue cleanup.
    }
  }

  async function runJob(job, controller) {
    var attachmentItem = null;
    job.attachmentTitle = getAttachmentTitle(null, job);
    var translationResult = null;
    var translationWarning = null;
    var translationRequestCount = null;
    var translationLogPath = null;
    var completedTitle = null;
    var succeeded = false;
    var skipped = false;
    var failure = null;
    try {
      attachmentItem = Zotero.Items.get(job.attachmentID);
      job.attachmentTitle = getAttachmentTitle(attachmentItem, job);
      if (!attachmentItem) throw new Error("PDF 附件已不存在或无法读取。");
      // A translated attachment may have been inserted after enqueueing but before this job starts.
      if (!ZoteroTranslateFilesAttachments.isTranslatablePDF(attachmentItem)) {
        skipped = true;
        return;
      }

      var settingsError = getSettingsError();
      if (settingsError) throw settingsError;

      openProgressWindow(job, attachmentItem);
      translationResult = await runner.translate(attachmentItem, {
        onProgress: function (progress) {
          if (!controller.signal.aborted && typeof api.onProgress === "function") {
            api.onProgress(job, progress);
          }
        },
        signal: controller.signal,
      });
      translationWarning = translationResult && translationResult.warning;
      translationLogPath = translationResult && translationResult.logPath;
      if (translationLogPath) job.logPath = translationLogPath;
      var rawRequestCount = translationResult && translationResult.requestCount;
      var reportedRequestCount = rawRequestCount == null ? NaN : Number(rawRequestCount);
      if (Number.isFinite(reportedRequestCount)) {
        translationRequestCount = Math.max(0, Math.floor(reportedRequestCount));
      }

      if (controller.signal.aborted) return;
      var outputPath = typeof translationResult === "string"
        ? translationResult
        : translationResult && translationResult.outputPath;
      if (!outputPath) throw new Error("BabelDOC 没有返回可导入的译本文件。");

      var resultImporter = importer;
      if (!resultImporter && ZoteroTranslateFiles && ZoteroTranslateFiles.modules) {
        resultImporter = ZoteroTranslateFiles.modules.importer;
      }
      if (resultImporter && typeof resultImporter.importResult === "function") {
        var translatedAttachment = await resultImporter.importResult(attachmentItem, outputPath);
        if (translatedAttachment && typeof translatedAttachment.getField === "function") {
          try {
            completedTitle = String(translatedAttachment.getField("title") || "");
          } catch (error) {
            completedTitle = null;
          }
        }
        if (!completedTitle) {
          completedTitle = String(attachmentItem.getField("title") || "PDF") + "（中文译本）";
        }
      } else {
        throw new Error("翻译结果导入模块尚未初始化。");
      }
      succeeded = true;
    } catch (error) {
      failure = error;
      if (error && error.logPath) job.logPath = error.logPath;
    } finally {
      if (currentProgress && currentProgress.job === job) stopProgressTimer(currentProgress);
      if (translationResult && typeof translationResult.cleanup === "function") {
        try {
          await translationResult.cleanup();
        } catch (cleanupError) {
          if (succeeded) {
            succeeded = false;
            failure = new Error("翻译已导入，但清理临时文件失败。");
          }
          log("temporary work directory cleanup failed for attachment " + job.attachmentID);
        }
      }

      if (!succeeded && !skipped && !controller.signal.aborted) {
        reportFailure(job, failure || new Error("翻译失败。"));
      }
      closeProgressWindow(
        job,
        succeeded,
        controller.signal.aborted,
        translationWarning,
        completedTitle,
        translationRequestCount,
        translationLogPath || job.logPath,
      );
    }
  }

  function startNext() {
    if (stopped || currentJob || maintenanceLocked) {
      notifyIdle();
      return;
    }

    var job = pending.shift();
    if (!job) {
      notifyIdle();
      return;
    }

    currentJob = job;
    currentController = makeAbortController();
    var controller = currentController;

    runJob(job, controller).finally(function () {
      queuedIDs.delete(String(job.attachmentID));
      if (currentJob === job) {
        currentJob = null;
        currentController = null;
      }
      if (stopped) notifyIdle();
      else startNext();
    });
  }

  var api = {
    init: function () {
      runner = ZoteroTranslateFilesRunner;
      stopped = false;
    },

    enqueue: function (attachmentIDs) {
      if (stopped) {
        showEnqueueNotice(0, 0, 0);
        return { addedCount: 0, duplicateCount: 0, aheadCount: 0 };
      }

      var beforeCount = pending.length + (currentJob ? 1 : 0);
      var addedCount = 0;
      var duplicateCount = 0;
      for (var attachmentID of attachmentIDs || []) {
        var numericID = Number(attachmentID);
        if (!Number.isInteger(numericID) || numericID <= 0) continue;

        var key = String(numericID);
        if (queuedIDs.has(key)) {
          duplicateCount++;
          continue;
        }

        queuedIDs.add(key);
        var queuedItem = null;
        try {
          queuedItem = Zotero.Items.get(numericID);
        } catch (error) {
          // Keep an ID-based fallback title if the attachment lookup already fails.
        }
        var job = { attachmentID: numericID };
        job.attachmentTitle = getAttachmentTitle(queuedItem, job);
        pending.push(job);
        addedCount++;
      }

      showEnqueueNotice(addedCount, beforeCount, duplicateCount);
      if (currentJob && currentProgress) refreshProgress(currentJob, currentProgress.lastProgress);
      if (addedCount) startNext();

      return {
        addedCount: addedCount,
        duplicateCount: duplicateCount,
        aheadCount: beforeCount,
        queueLength: pending.length + (currentJob ? 1 : 0),
      };
    },

    onIdle: function () {
      if (!currentJob && !pending.length) return Promise.resolve();
      return new Promise(function (resolve) {
        idleWaiters.push(resolve);
      });
    },

    acquireMaintenanceLock: function () {
      if (currentJob) return { acquired: false, reason: "translation" };
      if (maintenanceLocked) return { acquired: false, reason: "maintenance" };
      maintenanceLocked = true;
      var released = false;
      return {
        acquired: true,
        release: function () {
          if (released) return;
          released = true;
          maintenanceLocked = false;
          if (stopped) notifyIdle();
          else startNext();
        },
      };
    },

    cancelAll: function () {
      stopped = true;
      stopProgressTimer(currentProgress);
      for (var job of pending) queuedIDs.delete(String(job.attachmentID));
      pending = [];
      if (currentController) currentController.abort();
      notifyIdle();
    },

    shutdown: function () {
      api.cancelAll();
    },

    onJobFailed: function (job, error) {
      var failure = failureMessage(job, error);
      if (job && job.logPath) {
        formatLogPath(job.logPath).then(function (label) {
          scheduleFailureAlert(failure.message + "\n" + label);
        });
      } else {
        scheduleFailureAlert(failure.message);
      }
      try {
        if (Zotero && typeof Zotero.debug === "function") {
          Zotero.debug("BabelDOC for Zotero: " + failure.logMessage);
        } else {
          log(failure.logMessage);
        }
      } catch (logError) {
        // A diagnostic failure must not suppress the already scheduled alert.
      }
    },

    onProgress: function (job, progress) {
      refreshProgress(job, progress || {});
    },

    setImporter: function (value) {
      importer = value || null;
    },
  };

  try { Zotero.__babeldocForZoteroQueue = api; } catch (error) {
    // The installer can still use this module's direct global API when exposed.
  }

  return api;
})();
