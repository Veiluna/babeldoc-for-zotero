/* global Zotero, ZoteroTranslateFiles, ZoteroTranslateFilesConfig, ZoteroTranslateFilesRunner */

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

  function log(message) {
    if (ZoteroTranslateFiles && typeof ZoteroTranslateFiles.log === "function") {
      ZoteroTranslateFiles.log("queue: " + message);
    } else if (Zotero && typeof Zotero.debug === "function") {
      Zotero.debug("Zotero 中文 PDF 翻译 queue: " + message);
    }
  }

  function showNotice(descriptions) {
    try {
      var progress = new Zotero.ProgressWindow({ closeOnClick: true });
      progress.changeHeadline("Zotero 中文 PDF 翻译");
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
    if (!settings.babeldocPath) missing.push("BabelDOC 路径");
    if (!missing.length) return null;

    return new Error(
      "请先打开 Zotero 设置 → Zotero 中文 PDF 翻译，填写：" + missing.join("、") + "。"
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
    return stage + " · " + Math.round(overall) + "%（队列中还有 " + queueRemaining + " 篇）";
  }

  function refreshProgress(job, progress) {
    if (!currentProgress || currentProgress.job !== job) return;
    currentProgress.lastProgress = progress || currentProgress.lastProgress;
    if (!currentProgress.itemProgress) return;

    try {
      var overall = Number(currentProgress.lastProgress && currentProgress.lastProgress.overall);
      if (!Number.isFinite(overall)) overall = 0;
      currentProgress.itemProgress.setProgress(Math.max(0, Math.min(100, overall)));
      currentProgress.itemProgress.setText(
        progressText(currentProgress.lastProgress, pending.length)
      );
    } catch (error) {
      // Progress UI errors must not interrupt the translation queue.
    }
  }

  function openProgressWindow(job, attachmentItem) {
    var title = "PDF";
    try {
      title = String(attachmentItem.getField("title") || title);
    } catch (error) {
      // Keep the generic title if the attachment cannot provide one.
    }

    var entry = { job: job, window: null, itemProgress: null, lastProgress: {
      overall: 0,
      stage: "正在准备 BabelDOC",
    } };
    currentProgress = entry;
    try {
      entry.window = new Zotero.ProgressWindow({ closeOnClick: false });
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
      entry.itemProgress.setText(progressText(entry.lastProgress, pending.length));
      entry.window.show();
    } catch (error) {
      entry.window = null;
      entry.itemProgress = null;
      log("could not show translation progress window");
    }
  }

  function closeProgressWindow(job, succeeded, aborted) {
    if (!currentProgress || currentProgress.job !== job) return;
    var entry = currentProgress;
    currentProgress = null;
    if (!entry.window || !entry.itemProgress) return;

    try {
      if (succeeded) {
        entry.itemProgress.setProgress(100);
        entry.itemProgress.setText("翻译完成（队列中还有 " + pending.length + " 篇）");
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
    var translationResult = null;
    var succeeded = false;
    var failure = null;
    try {
      var settingsError = getSettingsError();
      if (settingsError) throw settingsError;

      var attachmentItem = Zotero.Items.get(job.attachmentID);
      if (!attachmentItem) throw new Error("PDF 附件已不存在或无法读取。");

      openProgressWindow(job, attachmentItem);
      translationResult = await runner.translate(attachmentItem, {
        onProgress: function (progress) {
          if (!controller.signal.aborted && typeof api.onProgress === "function") {
            api.onProgress(job, progress);
          }
        },
        signal: controller.signal,
      });

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
        await resultImporter.importResult(attachmentItem, outputPath);
      } else {
        throw new Error("翻译结果导入模块尚未初始化。");
      }
      succeeded = true;
    } catch (error) {
      failure = error;
    } finally {
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

      if (!succeeded && !controller.signal.aborted) {
        reportFailure(job, failure || new Error("翻译失败。"));
      }
      closeProgressWindow(job, succeeded, controller.signal.aborted);
    }
  }

  function startNext() {
    if (stopped || currentJob) {
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
        pending.push({ attachmentID: numericID });
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

    cancelAll: function () {
      stopped = true;
      for (var job of pending) queuedIDs.delete(String(job.attachmentID));
      pending = [];
      if (currentController) currentController.abort();
      notifyIdle();
    },

    shutdown: function () {
      api.cancelAll();
    },

    // Task 7 supplies the user-facing alert. Every failed job is still discarded.
    onJobFailed: function (job, error) {
      void error;
      log("translation failed for attachment " + job.attachmentID + "; details omitted");
    },

    onProgress: function (job, progress) {
      refreshProgress(job, progress || {});
    },

    setImporter: function (value) {
      importer = value || null;
    },
  };

  return api;
})();
