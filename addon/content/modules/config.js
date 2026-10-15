/* global Zotero */

// Shared by the preferences pane and the translation runner.
var ZoteroTranslateFilesConfig = (function () {
  var preferences = {
    baseURL: "extensions.zotero-translate-files.baseURL",
    apiKey: "extensions.zotero-translate-files.apiKey",
    model: "extensions.zotero-translate-files.model",
    babeldocPath: "extensions.zotero-translate-files.babeldocPath",
  };

  function getSettings() {
    function read(name) {
      var value = Zotero.Prefs.get(preferences[name], true);
      return String(value == null ? "" : value).trim();
    }

    return {
      baseURL: read("baseURL"),
      apiKey: read("apiKey"),
      model: read("model"),
      babeldocPath: read("babeldocPath"),
    };
  }

  // Sanitize diagnostics before they reach logs or user-facing alerts.
  function redact(value, apiKey) {
    var text;
    try {
      text = String(value == null ? "" : value);
    } catch (error) {
      text = "无法读取错误详情";
    }

    var configuredKey = apiKey;
    if (configuredKey == null) {
      try {
        configuredKey = getSettings().apiKey;
      } catch (error) {
        configuredKey = "";
      }
    }
    if (configuredKey) text = text.split(String(configuredKey)).join("[已隐藏]");

    return text
      .replace(/Bearer\s+\S+/gi, "Bearer [已隐藏]")
      .replace(/sk-[A-Za-z0-9_-]{8,}/g, "[已隐藏 API key]");
  }

  // The test endpoint is appended to the user's base URL unless already present.
  function chatCompletionsURL(baseURL) {
    var url = String(baseURL || "").trim().replace(/\/+$/, "");
    if (!url || /\/chat\/completions$/i.test(url)) return url;
    return url + "/chat/completions";
  }

  // BabelDOC expects the OpenAI base URL, so remove a full chat endpoint if supplied.
  function openAIBaseURLForBabelDOC(baseURL) {
    return String(baseURL || "")
      .trim()
      .replace(/\/+$/, "")
      .replace(/\/chat\/completions$/i, "");
  }

  // BabelDOC's Windows launcher lives in env\Scripts; Python is one directory above.
  function resolvePythonFromBabelDOCPath(path) {
    var value = String(path || "").trim().replace(/[\\/]+$/, "");
    if (!value) return "";

    var separatorIndex = Math.max(value.lastIndexOf("\\"), value.lastIndexOf("/"));
    var filename = value.slice(separatorIndex + 1);
    if (/^python\.exe$/i.test(filename)) return value;
    if (!/^babeldoc\.exe$/i.test(filename)) return "";

    var separator = value.indexOf("\\") >= 0 ? "\\" : "/";
    var parent = separatorIndex >= 0 ? value.slice(0, separatorIndex) : "";
    var parentName = parent.slice(Math.max(parent.lastIndexOf("\\"), parent.lastIndexOf("/")) + 1);

    if (/^Scripts$/i.test(parentName)) {
      var environment = parent.slice(0, Math.max(parent.lastIndexOf("\\"), parent.lastIndexOf("/")));
      if (environment) return environment + separator + "python.exe";
      if (/^[\\/]+$/.test(parent)) return separator + "python.exe";
      return "python.exe";
    }

    return (parent ? parent + separator : "") + "python.exe";
  }

  return {
    getSettings: getSettings,
    redact: redact,
    chatCompletionsURL: chatCompletionsURL,
    openAIBaseURLForBabelDOC: openAIBaseURLForBabelDOC,
    resolvePythonFromBabelDOCPath: resolvePythonFromBabelDOCPath,
  };
})();
