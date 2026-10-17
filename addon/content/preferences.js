/* global Zotero, ZoteroTranslateFilesInstaller */

var ZoteroTranslateFilesPreferences = (function () {
  var PANE_ID = "babeldoc-for-zotero-preferences";

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
    setMessage(document, statusID, "babeldoc-for-zotero-pref-api-http-error", {
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

  async function testAPI(document, window, settings) {
    var statusID = PANE_ID + "-api-status";
    if (!settings.baseURL || !settings.apiKey || !settings.model) {
      setMessage(document, statusID, "babeldoc-for-zotero-pref-api-missing");
      return;
    }

    var endpoint = ZoteroTranslateFilesConfig.chatCompletionsURL(settings.baseURL);
    if (urlContainsAPIKey(endpoint, settings.apiKey)) {
      setMessage(document, statusID, "babeldoc-for-zotero-pref-api-key-in-url");
      return;
    }

    try {
      var tokenParam = settings.tokenParam || "max_tokens";
      var response;
      var status;
      // At most one reshaped retry: a model that rejects the token-limit
      // parameter name answers with a no-cost 400 naming the other one.
      for (var attempt = 0; attempt < 2; attempt++) {
        var payload = {
          model: settings.model,
          messages: [{ role: "user", content: "ping" }],
        };
        payload[tokenParam] = 16;
        response = await Zotero.HTTP.request(
          "POST",
          endpoint,
          {
            headers: {
              "Content-Type": "application/json",
              Authorization: "Bearer " + settings.apiKey,
              "api-key": settings.apiKey,
            },
            body: JSON.stringify(payload),
            responseType: "text",
            successCodes: false,
            timeout: 30000,
          }
        );
        status = getHTTPStatus(response);
        var adapted = attempt === 0 &&
          typeof ZoteroTranslateFilesConfig.tokenLimitAdaptation === "function"
          ? ZoteroTranslateFilesConfig.tokenLimitAdaptation(status, response && response.responseText)
          : "";
        if (!adapted || adapted === tokenParam) break;
        tokenParam = adapted;
      }
      if (status >= 200 && status < 300 && tokenParam !== (settings.tokenParam || "max_tokens") &&
          typeof ZoteroTranslateFilesConfig.setTokenParam === "function") {
        // Remember the dialect so translations use it from the first request.
        ZoteroTranslateFilesConfig.setTokenParam(tokenParam);
      }
      if (status === 0) {
        setMessage(document, statusID, "babeldoc-for-zotero-pref-api-network-error");
      } else if (status >= 200 && status < 300) {
        setMessage(document, statusID, "babeldoc-for-zotero-pref-api-success", {
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
        setMessage(document, statusID, "babeldoc-for-zotero-pref-api-timeout");
      } else {
        // Avoid exception messages here because request details can contain the API key.
        setMessage(document, statusID, "babeldoc-for-zotero-pref-api-network-error");
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
          mainWindow.MozXULElement.insertFTLIfNeeded("babeldoc-for-zotero.ftl");
        }
        var label = await mainWindow.document.l10n.formatValue(
          "babeldoc-for-zotero-pref-pane-label"
        );
        if (label) return label;
      } catch (error) {
        // A localization lookup failure must not prevent the pane from registering.
      }
    }

    if (Zotero.Plugins && typeof Zotero.Plugins.getName === "function") {
      return Zotero.Plugins.getName(data.id);
    }
    return "BabelDOC for Zotero";
  }

  function showInstallProgress() {
    try {
      var progress = new Zotero.ProgressWindow({ closeOnClick: false });
      progress.changeHeadline("BabelDOC for Zotero");
      var item = new progress.ItemProgress("attachment", "BabelDOC 翻译环境");
      item.setProgress(0);
      item.setText("准备翻译环境");
      progress.show();
      return { window: progress, item: item };
    } catch (error) {
      return null;
    }
  }

  function updateInstallStatus(document) {
    var statusID = PANE_ID + "-environment-status";
    if (ZoteroTranslateFilesInstaller.isInstalling()) {
      setMessage(document, statusID, "babeldoc-for-zotero-pref-environment-installing");
      return;
    }
    ZoteroTranslateFilesInstaller.isReady().then(function (ready) {
      if (ZoteroTranslateFilesInstaller.isInstalling()) {
        setMessage(document, statusID, "babeldoc-for-zotero-pref-environment-installing");
      } else if (ready) setMessage(document, statusID, "babeldoc-for-zotero-pref-environment-ready");
      else setMessage(document, statusID, "babeldoc-for-zotero-pref-environment-missing");
    }).catch(function () {
      setMessage(document, statusID, ZoteroTranslateFilesInstaller.isInstalling()
        ? "babeldoc-for-zotero-pref-environment-installing"
        : "babeldoc-for-zotero-pref-environment-missing");
    });
  }

  function initializePane(document, window) {
    var pane = document.getElementById(PANE_ID);
    if (!pane || pane.dataset.initialized === "true") return;
    pane.dataset.initialized = "true";

    document.getElementById(PANE_ID + "-test").addEventListener("click", async function (event) {
      var button = event.currentTarget;
      button.disabled = true;
      setMessage(document, PANE_ID + "-api-status", "babeldoc-for-zotero-pref-api-checking");
      try {
        var settings = ZoteroTranslateFilesConfig.getSettings();
        await testAPI(document, window, settings);
      } catch (error) {
        var detail = error && error.message ? String(error.message) : "设置校验失败，请检查输入值。";
        setPlainText(document, PANE_ID + "-api-status", detail);
      } finally {
        button.disabled = false;
      }
    });

    var baseDir = "";
    try { baseDir = ZoteroTranslateFilesInstaller.baseDir(); } catch (error) { /* The status explains the missing environment. */ }
    var basePath = document.getElementById(PANE_ID + "-environment-path");
    if (basePath && document.l10n && typeof document.l10n.setAttributes === "function") {
      document.l10n.setAttributes(basePath, "babeldoc-for-zotero-pref-environment-path", { path: baseDir });
    } else if (basePath) {
      basePath.textContent = "环境目录：" + baseDir;
    }
    if (typeof ZoteroTranslateFilesInstaller.addStateListener === "function") {
      var removeStateListener = ZoteroTranslateFilesInstaller.addStateListener(function (running) {
        if (running) setMessage(document, PANE_ID + "-environment-status", "babeldoc-for-zotero-pref-environment-installing");
        else updateInstallStatus(document);
      });
      if (window && typeof window.addEventListener === "function") {
        window.addEventListener("unload", removeStateListener, { once: true });
      }
    }

    document.getElementById(PANE_ID + "-install").addEventListener("click", async function (event) {
      var button = event.currentTarget;
      button.disabled = true;
      var progress = null;
      try {
        var installOptions = {
          source: ZoteroTranslateFilesConfig.getDownloadSource(),
          onStart: function () {
            setMessage(document, PANE_ID + "-environment-status", "babeldoc-for-zotero-pref-environment-installing");
            progress = showInstallProgress();
          },
          onProgress: function (value) {
            if (!progress || !progress.item) return;
            progress.item.setProgress(value.overall);
            progress.item.setText(value.stage);
          },
        };
        var pythonPath = await ZoteroTranslateFilesInstaller.repairEnvironment(installOptions);
        if (!pythonPath) {
          updateInstallStatus(document);
          return;
        }
        if (progress && progress.item) {
          progress.item.setProgress(100);
          progress.item.setText("BabelDOC 环境已就绪。");
          progress.window.startCloseTimer(4000);
        }
        setMessage(document, PANE_ID + "-environment-status", "babeldoc-for-zotero-pref-environment-ready");
      } catch (error) {
        var detail = ZoteroTranslateFilesConfig.redact(error && error.message ? error.message : error);
        var repairMessageIDs = {
          TRANSLATION_RUNNING: "babeldoc-for-zotero-pref-environment-repair-busy",
          ENVIRONMENT_MAINTENANCE_BUSY: "babeldoc-for-zotero-pref-environment-maintenance-busy",
          ENVIRONMENT_DELETE_FAILED: "babeldoc-for-zotero-pref-environment-repair-delete-failed",
          ENVIRONMENT_DELETE_PROTECTED: "babeldoc-for-zotero-pref-environment-repair-logs-protected",
          REPAIR_QUEUE_UNAVAILABLE: "babeldoc-for-zotero-pref-environment-repair-queue-unavailable",
        };
        var repairMessageID = error && repairMessageIDs[error.code];
        if (repairMessageID) setMessage(document, PANE_ID + "-environment-status", repairMessageID);
        else setPlainText(document, PANE_ID + "-environment-status", "环境安装失败：" + detail);
        if (progress && progress.item) {
          if (typeof progress.item.setError === "function") progress.item.setError();
          progress.item.setText("环境安装失败。");
          progress.window.startCloseTimer(5000);
        }
      } finally {
        button.disabled = false;
      }
    });

    updateInstallStatus(document);
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
              data.rootURI + "content/modules/installer.js",
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
