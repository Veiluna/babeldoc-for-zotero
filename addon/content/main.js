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
    this.startReaderSelection();
    this.log("initialized " + this.version);
  },

  // 分栏双语划词隔离（#44）：模块只对带 ZTFSplitLayout 元数据的 PDF 挂钩，
  // 新打开的阅读器经公开事件 renderToolbar 通知，已打开的阅读器启动时补挂。
  readerToolbarHandler: null,

  // 划词钩子诊断：统一调试前缀后接一行可解析的 JSON（模块传来的行已脱敏，原样输出），
  // 只记录事件名、原因与异常类型，不含异常消息、路径或 PDF 文本。
  logReaderSelection(reason, error) {
    var entry = { event: "reader_selection_wiring", reason: reason, count: 1 };
    if (error) entry.error = error && error.name ? String(error.name) : "error";
    this.debugLine(JSON.stringify(entry));
  },

  debugLine(line) {
    if (typeof Zotero !== "undefined" && Zotero.debug) Zotero.debug("BabelDOC for Zotero: " + line);
  },

  startReaderSelection() {
    if (typeof ZoteroTranslateFilesReaderSelection === "undefined") {
      this.logReaderSelection("module_unavailable");
      return;
    }
    if (typeof Zotero === "undefined" || !Zotero.Reader) {
      this.logReaderSelection("reader_api_unavailable");
      return;
    }
    var selection = ZoteroTranslateFilesReaderSelection;
    var plugin = this;
    try {
      selection.init({ log: function (line) { plugin.debugLine(line); } });
      var attach = function (reader) {
        Promise.resolve(selection.attachToReader(reader)).catch(function (error) {
          plugin.logReaderSelection("attach_failed", error);
        });
      };
      this.readerToolbarHandler = function (event) { attach(event && event.reader); };
      Zotero.Reader.registerEventListener("renderToolbar", this.readerToolbarHandler, this.id);
      if (!Array.isArray(Zotero.Reader._readers)) {
        // 已打开阅读器列表是私有字段；取不到时只影响补挂，新打开的阅读器仍会挂钩。
        this.logReaderSelection("open_readers_unavailable");
        return;
      }
      for (var reader of Zotero.Reader._readers) attach(reader);
    } catch (error) {
      this.logReaderSelection("start_failed", error);
    }
  },

  stopReaderSelection() {
    if (typeof Zotero !== "undefined" && Zotero.Reader && this.readerToolbarHandler) {
      try {
        Zotero.Reader.unregisterEventListener("renderToolbar", this.readerToolbarHandler);
      } catch (error) {
        this.logReaderSelection("unregister_failed", error);
      }
    }
    this.readerToolbarHandler = null;
    if (typeof ZoteroTranslateFilesReaderSelection !== "undefined") {
      try {
        ZoteroTranslateFilesReaderSelection.shutdown();
      } catch (error) {
        this.logReaderSelection("shutdown_failed", error);
      }
    }
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
    this.stopReaderSelection();
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
