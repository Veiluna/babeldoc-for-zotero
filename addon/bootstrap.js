/* global Zotero, Services */

var ZoteroTranslateFiles;
var addonData;
var startupGeneration = 0;

function log(message) {
  if (typeof Zotero !== "undefined" && Zotero.debug) {
    Zotero.debug("Zotero 中文 PDF 翻译: " + message);
  }
}

function install() {
  log("installed");
}

async function startup({ id, version, rootURI }) {
  var generation = ++startupGeneration;
  addonData = { id, version, rootURI };
  log("starting " + version);

  try {
    // Bootstrap startup can race with Zotero initialization on some paths.
    await Zotero.initializationPromise;
    if (generation !== startupGeneration) return;

    Services.scriptloader.loadSubScript(rootURI + "content/modules/config.js");
    Services.scriptloader.loadSubScript(rootURI + "content/preferences.js");
    Services.scriptloader.loadSubScript(rootURI + "content/main.js");
    ZoteroTranslateFiles.init(addonData);

    // A plugin may be enabled after Zotero's main window has already loaded.
    for (var window of Zotero.getMainWindows()) {
      onMainWindowLoad({ window });
    }
  } catch (error) {
    log("startup failed: " + (error && error.stack ? error.stack : error));
    if (generation === startupGeneration) shutdown();
  }
}

function onMainWindowLoad({ window }) {
  if (!ZoteroTranslateFiles || !window || window.closed) return;

  // FTL files are auto-registered by Zotero from locale/<locale>/.
  window.MozXULElement.insertFTLIfNeeded("zotero-translate-files.ftl");
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
    'link[rel="localization"][href="zotero-translate-files.ftl"]'
  );
  if (link) link.remove();
}

function shutdown() {
  // Invalidate a startup still waiting on initializationPromise.
  startupGeneration++;

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
  log("uninstalled");
}
