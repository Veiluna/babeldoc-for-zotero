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

  function setPlainText(document, elementID, text) {
    var element = document.getElementById(elementID);
    if (!element) return;
    element.removeAttribute("data-l10n-id");
    element.removeAttribute("data-l10n-args");
    element.textContent = String(text || "设置校验失败，请检查输入值。");
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
    var candidates = [
      responseOrError && responseOrError.status,
      responseOrError && responseOrError.xhr && responseOrError.xhr.status,
      responseOrError && responseOrError.xmlhttp && responseOrError.xmlhttp.status,
      responseOrError && responseOrError.response && responseOrError.response.status,
    ];
    for (var candidate of candidates) {
      var status = Number(candidate);
      if (Number.isFinite(status) && status > 0) return status;
    }
    return 0;
  }

  function getHTTPResponseText(responseOrError) {
    var xhr = responseOrError && (
      responseOrError.xhr || responseOrError.xmlhttp || responseOrError.response
    );
    var responseText = xhr && xhr.responseText;
    if (responseText == null && responseOrError) responseText = responseOrError.responseText;
    return String(responseText == null ? "" : responseText);
  }

  function urlContainsAPIKey(url, apiKey) {
    var endpoint = String(url == null ? "" : url);
    var key = String(apiKey == null ? "" : apiKey);
    if (!key) return false;

    if (endpoint.indexOf(key) !== -1) return true;

    // Zotero's HTTP layer may log the request URL before this pane can redact it.
    // Check both common encoded forms and decoded URLs before handing it off.
    var encodedKeys = [encodeURIComponent(key), encodeURI(key)];
    encodedKeys.push(encodeURIComponent(key).replace(/%20/g, "+"));
    var normalizedEndpoint = endpoint.replace(/%[0-9a-f]{2}/gi, function (escape) {
      return escape.toUpperCase();
    });
    for (var encodedKey of encodedKeys) {
      var normalizedKey = encodedKey.replace(/%[0-9a-f]{2}/gi, function (escape) {
        return escape.toUpperCase();
      });
      if (normalizedEndpoint.indexOf(normalizedKey) !== -1) return true;
    }

    var decodedEndpoint = endpoint;
    for (var pass = 0; pass < 3; pass++) {
      var nextEndpoint;
      try {
        nextEndpoint = decodeURIComponent(decodedEndpoint.replace(/\+/g, "%20"));
      } catch (error) {
        break;
      }
      if (nextEndpoint === decodedEndpoint) break;
      if (nextEndpoint.indexOf(key) !== -1) return true;
      decodedEndpoint = nextEndpoint;
    }

    return false;
  }

  function isTimeoutError(error) {
    var TimeoutException = typeof Zotero !== "undefined" && Zotero.HTTP &&
      Zotero.HTTP.TimeoutException;
    if (typeof TimeoutException === "function" && error instanceof TimeoutException) {
      return true;
    }
    var name = error && error.name ? String(error.name) : "";
    var message = error && error.message ? String(error.message) : "";
    return /timeout|timed out/i.test(name + " " + message);
  }

  function setHTTPError(document, statusID, responseOrError, apiKey) {
    setMessage(document, statusID, "zotero-translate-files-pref-api-http-error", {
      status: getHTTPStatus(responseOrError) || "?",
      body: shortErrorBody(getHTTPResponseText(responseOrError), apiKey),
    });
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

    var endpoint = ZoteroTranslateFilesConfig.chatCompletionsURL(settings.baseURL);
    if (urlContainsAPIKey(endpoint, settings.apiKey)) {
      setMessage(document, statusID, "zotero-translate-files-pref-api-key-in-url");
      return;
    }

    try {
      var response = await Zotero.HTTP.request(
        "POST",
        endpoint,
        {
          headers: {
            "Content-Type": "application/json",
            Authorization: "Bearer " + settings.apiKey,
            "api-key": settings.apiKey,
          },
          body: JSON.stringify({
            model: settings.model,
            messages: [{ role: "user", content: "ping" }],
            max_tokens: 1,
          }),
          responseType: "text",
          successCodes: false,
          timeout: 30000,
        }
      );
      var status = getHTTPStatus(response);
      if (status === 0) {
        setMessage(document, statusID, "zotero-translate-files-pref-api-network-error");
      } else if (status >= 200 && status < 300) {
        setMessage(document, statusID, "zotero-translate-files-pref-api-success", {
          status: status,
        });
      } else {
        setHTTPError(document, statusID, response, settings.apiKey);
      }
    } catch (error) {
      var status = getHTTPStatus(error);
      if (status) {
        setHTTPError(document, statusID, error, settings.apiKey);
      } else if (isTimeoutError(error)) {
        setMessage(document, statusID, "zotero-translate-files-pref-api-timeout");
      } else {
        // Avoid exception messages here because request details can contain the API key.
        setMessage(document, statusID, "zotero-translate-files-pref-api-network-error");
      }
    }
  }

  async function getPreferencePaneLabel(data) {
    var mainWindow = null;
    try {
      if (typeof Zotero.getMainWindow === "function") mainWindow = Zotero.getMainWindow();
    } catch (error) {
      mainWindow = null;
    }

    if (mainWindow && !mainWindow.closed && mainWindow.document && mainWindow.document.l10n &&
        typeof mainWindow.document.l10n.formatValue === "function") {
      try {
        if (mainWindow.MozXULElement &&
            typeof mainWindow.MozXULElement.insertFTLIfNeeded === "function") {
          mainWindow.MozXULElement.insertFTLIfNeeded("zotero-translate-files.ftl");
        }
        var label = await mainWindow.document.l10n.formatValue(
          "zotero-translate-files-pref-pane-label"
        );
        if (label) return label;
      } catch (error) {
        // A localization lookup failure must not prevent the pane from registering.
      }
    }

    if (Zotero.Plugins && typeof Zotero.Plugins.getName === "function") {
      return Zotero.Plugins.getName(data.id);
    }
    return "Translate Files for Zotero";
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
      try {
        var settings = ZoteroTranslateFilesConfig.getSettings();
        setPathStatus(document, settings);
        await testAPI(document, window, settings);
      } catch (error) {
        var detail = error && error.message ? String(error.message) : "设置校验失败，请检查输入值。";
        setPlainText(document, PANE_ID + "-api-status", detail);
      } finally {
        button.disabled = false;
      }
    });
  }

  return {
    init: function (data) {
      // Zotero unregisters this pane when the plugin shuts down.
      try {
        var registration = getPreferencePaneLabel(data).then(function (label) {
          return Zotero.PreferencePanes.register({
            pluginID: data.id,
            src: data.rootURI + "content/preferences.xhtml",
            label: label,
            image: data.rootURI + "content/icons/icon.svg",
            scripts: [
              data.rootURI + "content/modules/config.js",
              data.rootURI + "content/preferences.js",
            ],
          });
        });
        registration.catch(function (error) {
          ZoteroTranslateFiles.log("preference pane registration failed: " + error);
        });
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
