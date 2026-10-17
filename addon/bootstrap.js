/* global Zotero, ZoteroTranslateFilesInstaller, ZoteroTranslateFilesConfig, Services */

var ZoteroTranslateFiles;
var addonData;
var startupGeneration = 0;
var startupInstallController = null;
var startupProgress = null;

function log(message) {
  if (typeof Zotero !== "undefined" && Zotero.debug) {
    Zotero.debug("BabelDOC for Zotero: " + message);
  }
}

function install() {
  log("installed");
}

async function startup({ id, version, rootURI }) {
  var generation = ++startupGeneration;
  stopStartupInstall();
  addonData = { id, version, rootURI };
  log("starting " + version);

  try {
    // Bootstrap startup can race with Zotero initialization on some paths.
    await Zotero.initializationPromise;
    if (generation !== startupGeneration) return;

    Services.scriptloader.loadSubScript(rootURI + "content/modules/config.js");
    Services.scriptloader.loadSubScript(rootURI + "content/modules/installer.js");
    if (typeof ZoteroTranslateFilesInstaller.activate === "function") {
      ZoteroTranslateFilesInstaller.activate();
    }
    Services.scriptloader.loadSubScript(rootURI + "content/modules/attachments.js");
    Services.scriptloader.loadSubScript(rootURI + "content/modules/importer.js");
    Services.scriptloader.loadSubScript(rootURI + "content/preferences.js");
    Services.scriptloader.loadSubScript(rootURI + "content/modules/runner.js");
    Services.scriptloader.loadSubScript(rootURI + "content/modules/queue.js");
    Services.scriptloader.loadSubScript(rootURI + "content/modules/menu.js");
    Services.scriptloader.loadSubScript(rootURI + "content/main.js");
    ZoteroTranslateFiles.init(addonData);

    // A plugin may be enabled after Zotero's main window has already loaded.
    for (var window of Zotero.getMainWindows()) {
      onMainWindowLoad({ window });
    }

    prepareEnvironmentAtStartup(generation);
  } catch (error) {
    log("startup failed: " + (error && error.stack ? error.stack : error));
    if (generation === startupGeneration) shutdown();
  }
}

function getOpenMainWindow() {
  try {
    for (var window of Zotero.getMainWindows()) {
      if (window && !window.closed) return window;
    }
  } catch (error) {
    // Fall back to Zotero's singular main-window accessor.
  }
  try {
    var mainWindow = Zotero.getMainWindow();
    return mainWindow && !mainWindow.closed ? mainWindow : null;
  } catch (error) {
    return null;
  }
}

function redactStartupError(error) {
  var message = error && error.message ? String(error.message) : String(error || "unknown error");
  try {
    if (ZoteroTranslateFilesConfig && typeof ZoteroTranslateFilesConfig.redact === "function") {
      return ZoteroTranslateFilesConfig.redact(message);
    }
  } catch (redactionError) {
    // Do not expose raw exception text if redaction is unavailable.
  }
  return "error details unavailable";
}

function localizeStartupText(mainWindow, messageID, args) {
  var localization = mainWindow && mainWindow.document && mainWindow.document.l10n;
  if (!localization || typeof localization.formatValue !== "function") {
    return Promise.resolve(messageID);
  }
  try {
    return Promise.resolve(localization.formatValue(messageID, args)).then(function (value) {
      return value || messageID;
    }, function () {
      return messageID;
    });
  } catch (error) {
    return Promise.resolve(messageID);
  }
}

function setStartupStage(progress, mainWindow, generation, value) {
  var sequence = ++progress.sequence;
  var stepID = "babeldoc-for-zotero-installer-step-" + value.stepId;
  localizeStartupText(mainWindow, stepID).then(function (step) {
    return localizeStartupText(mainWindow, "babeldoc-for-zotero-startup-progress-stage", {
      stepIndex: value.stepIndex,
      stepCount: value.stepCount,
      step: step,
    });
  }).then(function (text) {
    if (generation !== startupGeneration || startupProgress !== progress ||
        progress.sequence !== sequence) return;
    progress.item.setText(text);
  }).catch(function () {
    // Localization failures must not interrupt the installer.
  });
}

async function openStartupInstallProgress(mainWindow, generation) {
  try {
    if (mainWindow.MozXULElement && typeof mainWindow.MozXULElement.insertFTLIfNeeded === "function") {
      mainWindow.MozXULElement.insertFTLIfNeeded("babeldoc-for-zotero.ftl");
    }
    var itemLabel = await localizeStartupText(mainWindow, "babeldoc-for-zotero-startup-progress-item");
    var initialText = await localizeStartupText(mainWindow, "babeldoc-for-zotero-startup-progress-initial");
    if (generation !== startupGeneration || !ZoteroTranslateFiles) return null;

    var progress = new Zotero.ProgressWindow({ closeOnClick: false });
    progress.changeHeadline("BabelDOC for Zotero");
    var item = new progress.ItemProgress("attachment", itemLabel);
    item.setProgress(0);
    item.setText(initialText);
    progress.show();
    return { window: progress, item: item, sequence: 0 };
  } catch (error) {
    log("could not show startup install progress window");
    return null;
  }
}

async function prepareEnvironmentAtStartup(generation) {
  try {
    await Zotero.uiReadyPromise;
    if (generation !== startupGeneration || !ZoteroTranslateFiles) return;

    var mainWindow = getOpenMainWindow();
    if (!mainWindow) return;

    var ready = await ZoteroTranslateFilesInstaller.isReady();
    if (generation !== startupGeneration || !ZoteroTranslateFiles || ready) return;

    var controller = ZoteroTranslateFilesInstaller.createInstallAbortController();
    var progress = await openStartupInstallProgress(mainWindow, generation);
    if (generation !== startupGeneration || !ZoteroTranslateFiles) {
      if (progress && progress.window) {
        try { progress.window.close(); } catch (error) { /* Shutdown must continue. */ }
      }
      return;
    }
    startupInstallController = controller;
    startupProgress = progress;

    try {
      await ZoteroTranslateFilesInstaller.ensureReady({
        source: ZoteroTranslateFilesConfig.getDownloadSource(),
        interactive: false,
        signal: controller.signal,
        onProgress: function (value) {
          if (!progress || !progress.item || generation !== startupGeneration) return;
          progress.item.setProgress(value.overall);
          setStartupStage(progress, mainWindow, generation, value);
        },
      });
      if (generation !== startupGeneration || controller.signal.aborted) return;
      if (progress && progress.item) {
        progress.item.setProgress(100);
        var successSequence = ++progress.sequence;
        var successText = await localizeStartupText(mainWindow, "babeldoc-for-zotero-startup-ready");
        if (generation !== startupGeneration || controller.signal.aborted ||
            progress.sequence !== successSequence) return;
        progress.item.setText(successText);
        progress.window.startCloseTimer(4000);
      }
    } catch (error) {
      if (generation !== startupGeneration || controller.signal.aborted) return;
      var logPath = error && error.logPath ? String(error.logPath) : "";
      log("startup install failed: " + redactStartupError(error));
      if (progress && progress.item) {
        if (typeof progress.item.setError === "function") progress.item.setError();
        var failureSequence = ++progress.sequence;
        var failureText = await localizeStartupText(mainWindow, "babeldoc-for-zotero-startup-failed", {
          path: logPath,
        });
        if (generation !== startupGeneration || controller.signal.aborted ||
            progress.sequence !== failureSequence) return;
        progress.item.setText(failureText);
        progress.window.startCloseTimer(8000);
      }
    } finally {
      if (startupInstallController === controller) startupInstallController = null;
    }
  } catch (error) {
    if (generation === startupGeneration) {
      log("startup environment check failed: " + redactStartupError(error));
    }
  }
}

function stopStartupInstall() {
  if (startupInstallController && !startupInstallController.signal.aborted) {
    startupInstallController.abort();
  }
  startupInstallController = null;
  if (startupProgress && startupProgress.window) {
    try { startupProgress.window.close(); } catch (error) { /* Shutdown must continue. */ }
  }
  startupProgress = null;
}

function onMainWindowLoad({ window }) {
  if (!ZoteroTranslateFiles || !window || window.closed) return;

  // FTL files are auto-registered by Zotero from locale/<locale>/.
  window.MozXULElement.insertFTLIfNeeded("babeldoc-for-zotero.ftl");
  ZoteroTranslateFiles.addToWindow(window);
}

function onMainWindowUnload({ window }) {
  if (!window) return;
  if (ZoteroTranslateFiles) ZoteroTranslateFiles.removeFromWindow(window);
  removeFTLFromWindow(window);
}

function removeFTLFromWindow(window) {
  if (!window || !window.document) return;
  var link = window.document.querySelector(
    'link[rel="localization"][href="babeldoc-for-zotero.ftl"]'
  );
  if (link) link.remove();
}

function shutdown() {
  // Invalidate a startup still waiting on initializationPromise.
  startupGeneration++;
  stopStartupInstall();
  if (typeof ZoteroTranslateFilesInstaller !== "undefined" &&
      typeof ZoteroTranslateFilesInstaller.cancelInFlight === "function") {
    ZoteroTranslateFilesInstaller.cancelInFlight();
  }

  if (ZoteroTranslateFiles) {
    var windows = typeof Zotero !== "undefined" && Zotero.getMainWindows
      ? Zotero.getMainWindows()
      : [];
    ZoteroTranslateFiles.shutdown();
    for (var window of windows) removeFTLFromWindow(window);
    ZoteroTranslateFiles = undefined;
  }

  addonData = undefined;
  log("shut down");
}

function uninstall() {
  startupGeneration++;
  stopStartupInstall();
  if (typeof ZoteroTranslateFilesInstaller !== "undefined" &&
      typeof ZoteroTranslateFilesInstaller.cancelInFlight === "function") {
    ZoteroTranslateFilesInstaller.cancelInFlight();
  }
  log("uninstalled");
}
