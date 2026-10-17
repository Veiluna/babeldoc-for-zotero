/* global Zotero */

var ZoteroTranslateFiles = {
  id: null,
  version: null,
  rootURI: null,
  started: false,
  windows: new Map(),

  // Bootstrap loads the feature modules before init() and dispatches lifecycle hooks here.
  modules: {
    preferences: null,
    menu: null,
    queue: null,
    runner: null,
    importer: null,
  },

  init(data) {
    ZoteroTranslateFilesConfig.migrateLegacyPreferences();
    this.id = data.id;
    this.version = data.version;
    this.rootURI = data.rootURI;
    this.modules.preferences = ZoteroTranslateFilesPreferences;
    this.modules.menu = ZoteroTranslateFilesMenu;
    this.modules.queue = ZoteroTranslateFilesQueue;
    this.modules.runner = ZoteroTranslateFilesRunner;
    this.modules.importer = ZoteroTranslateFilesImporter;
    this.started = true;
    this.callModules("init", [data]);
    this.log("initialized " + this.version);
  },

  addToWindow(window) {
    if (!this.started || !window || this.windows.has(window)) return;
    this.windows.set(window, true);
    this.callModules("onMainWindowLoad", [{ window }]);
  },

  removeFromWindow(window) {
    if (!window || !this.windows.has(window)) return;
    this.callModules("onMainWindowUnload", [{ window }], true);
    this.windows.delete(window);
  },

  removeFromAllWindows() {
    for (var window of Array.from(this.windows.keys())) {
      this.removeFromWindow(window);
    }
  },

  shutdown() {
    this.removeFromAllWindows();
    this.callModules("shutdown", [], true);
    this.started = false;
    this.id = null;
    this.version = null;
    this.rootURI = null;
    this.log("shutdown complete");
  },

  callModules(hook, args, reverse) {
    var names = Object.keys(this.modules);
    if (reverse) names.reverse();

    for (var name of names) {
      var module = this.modules[name];
      if (module && typeof module[hook] === "function") {
        try {
          module[hook].apply(module, args);
        } catch (error) {
          this.log(name + "." + hook + " failed: " + error);
        }
      }
    }
  },

  log(message) {
    if (typeof Zotero !== "undefined" && Zotero.debug) {
      Zotero.debug("BabelDOC for Zotero: " + message);
    }
  },
};
