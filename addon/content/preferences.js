/* global Zotero, ChromeUtils */

var ZoteroTranslateFilesPreferences = (function () {
  var PANE_ID = "zotero-translate-files-preferences";

  function setMessage(document, elementID, messageID, args) {
    var element = document.getElementById(elementID);
    if (!element) return;

    if (document.l10n && typeof document.l10n.setAttributes === "function") {
      document.l10n.setAttributes(element, messageID, args);
      return;
    }

    element.setAttribute("data-l10n-id", messageID);
    if (args) element.setAttribute("data-l10n-args", JSON.stringify(args));
    else element.removeAttribute("data-l10n-args");
  }

  function pathIsFile(path) {
    if (!path) return false;
    try {
      var file = Zotero.File.pathToFile(path);
      return file.exists() && file.isFile();
    } catch (error) {
      return false;
    }
  }

  function getHTTPStatus(responseOrError) {
    var status = responseOrError && responseOrError.status;
    if (!status && responseOrError && responseOrError.xhr) {
      status = responseOrError.xhr.status;
    }
    status = Number(status);
    return Number.isFinite(status) && status > 0 ? status : 0;
  }

  function redactString(value, apiKey) {
    return apiKey ? value.split(apiKey).join("[已隐藏]") : value;
  }

  function redactJSON(value, apiKey) {
    if (typeof value === "string") return redactString(value, apiKey);
    if (Array.isArray(value)) {
      return value.map(function (item) {
        return redactJSON(item, apiKey);
      });
    }
    if (value && typeof value === "object") {
      var redacted = Object.create(null);
      Object.keys(value).forEach(function (key) {
        redacted[redactString(key, apiKey)] = redactJSON(value[key], apiKey);
      });
      return redacted;
    }
    return value;
  }

  function shortErrorBody(body, apiKey) {
    var raw = String(body == null ? "" : body);
    var formatted;
    try {
      // Parse first so JSON escapes (for example \/) are decoded before redaction.
      formatted = JSON.stringify(redactJSON(JSON.parse(raw), apiKey));
    } catch (error) {
      // Non-JSON responses still need redaction before whitespace cleanup and truncation.
      formatted = redactString(raw, apiKey);
    }

    var value = formatted.replace(/[\r\n\t]+/g, " ").replace(/\s{2,}/g, " ").trim();
    value = redactString(value, apiKey);
    return value.slice(0, 240) || "（无响应正文）";
  }

  function setPathStatus(document, settings) {
    var path = settings.babeldocPath;
    if (!path) {
      setMessage(document, PANE_ID + "-path-status", "zotero-translate-files-pref-path-empty");
      return;
    }

    if (!pathIsFile(path)) {
      setMessage(document, PANE_ID + "-path-status", "zotero-translate-files-pref-path-babeldoc-missing");
      return;
    }

    var pythonPath = ZoteroTranslateFilesConfig.resolvePythonFromBabelDOCPath(path);
    if (!pythonPath) {
      setMessage(document, PANE_ID + "-path-status", "zotero-translate-files-pref-path-unrecognized");
      return;
    }

    if (!pathIsFile(pythonPath)) {
      setMessage(document, PANE_ID + "-path-status", "zotero-translate-files-pref-path-python-missing", {
        path: pythonPath,
      });
      return;
    }

    setMessage(document, PANE_ID + "-path-status", "zotero-translate-files-pref-path-success", {
      path: pythonPath,
    });
  }

  async function testAPI(document, window, settings) {
    var statusID = PANE_ID + "-api-status";
    if (!settings.baseURL || !settings.apiKey || !settings.model) {
      setMessage(document, statusID, "zotero-translate-files-pref-api-missing");
      return;
    }

    var controller = new window.AbortController();
    var timeoutID = window.setTimeout(function () {
      controller.abort();
    }, 30000);

    try {
      var response = await window.fetch(
        ZoteroTranslateFilesConfig.chatCompletionsURL(
          ZoteroTranslateFilesConfig.openAIBaseURLForBabelDOC(settings.baseURL)
        ),
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: "Bearer " + settings.apiKey,
          },
          body: JSON.stringify({
            model: settings.model,
            messages: [{ role: "user", content: "ping" }],
            max_tokens: 1,
          }),
          signal: controller.signal,
          credentials: "omit",
        }
      );
      var status = getHTTPStatus(response);
      if (status >= 200 && status < 300) {
        setMessage(document, statusID, "zotero-translate-files-pref-api-success", {
          status: status,
        });
      } else {
        var body = "";
        try {
          body = await response.text();
        } catch (error) {
          // Keep the HTTP status visible even if its short error body cannot be read.
        }
        setMessage(document, statusID, "zotero-translate-files-pref-api-http-error", {
          status: status || "?",
          body: shortErrorBody(body, settings.apiKey),
        });
      }
    } catch (error) {
      // Avoid exception messages here because request details can contain the API key.
      setMessage(document, statusID, "zotero-translate-files-pref-api-network-error");
    } finally {
      window.clearTimeout(timeoutID);
    }
  }

  async function pickBabelDOCPath(document, window) {
    var title = "Select BabelDOC executable";
    var filterTitle = "Executable files (*.exe)";
    if (document.l10n && typeof document.l10n.formatValue === "function") {
      title = await document.l10n.formatValue("zotero-translate-files-pref-picker-title");
      filterTitle = await document.l10n.formatValue("zotero-translate-files-pref-picker-filter");
    }

    var picker = new (ChromeUtils.importESModule(
      "chrome://zotero/content/modules/filePicker.mjs"
    ).FilePicker)();
    picker.init(window, title, picker.modeOpen);
    picker.appendFilter(filterTitle, "*.exe");

    var result = await picker.show();
    if (result !== picker.returnOK || !picker.file) return;

    var input = document.getElementById(PANE_ID + "-babeldoc-path");
    input.value = picker.file;
    // Zotero's preference binding listens to change/input events for XHTML controls.
    input.dispatchEvent(new window.Event("change", { bubbles: true }));
  }

  function initializePane(document, window) {
    var pane = document.getElementById(PANE_ID);
    if (!pane || pane.dataset.initialized === "true") return;
    pane.dataset.initialized = "true";

    document.getElementById(PANE_ID + "-browse").addEventListener("click", function () {
      pickBabelDOCPath(document, window).catch(function () {
        setMessage(document, PANE_ID + "-path-status", "zotero-translate-files-pref-path-picker-error");
      });
    });

    document.getElementById(PANE_ID + "-test").addEventListener("click", async function (event) {
      var button = event.currentTarget;
      button.disabled = true;
      setMessage(document, PANE_ID + "-api-status", "zotero-translate-files-pref-api-checking");
      setPathStatus(document, ZoteroTranslateFilesConfig.getSettings());
      try {
        await testAPI(document, window, ZoteroTranslateFilesConfig.getSettings());
      } finally {
        button.disabled = false;
      }
    });
  }

  return {
    init: function (data) {
      // Zotero unregisters this pane when the plugin shuts down.
      try {
        var registration = Zotero.PreferencePanes.register({
          pluginID: data.id,
          src: data.rootURI + "content/preferences.xhtml",
          label: "Zotero 中文 PDF 翻译",
          image: data.rootURI + "content/icons/icon.svg",
          scripts: [
            data.rootURI + "content/modules/config.js",
            data.rootURI + "content/preferences.js",
          ],
        });
        if (registration && typeof registration.catch === "function") {
          registration.catch(function (error) {
            ZoteroTranslateFiles.log("preference pane registration failed: " + error);
          });
        }
      } catch (error) {
        ZoteroTranslateFiles.log("preference pane registration failed: " + error);
      }
    },
    initializePane: initializePane,
  };
})();

// Preference pane scripts run in the settings window's scope; bootstrap may load
// this same file in a scope without a window global.
if (typeof window !== "undefined") {
  window.ZoteroTranslateFilesPreferences = ZoteroTranslateFilesPreferences;
}
