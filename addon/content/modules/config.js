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

  // Normalize OpenAI-compatible roots while preserving concrete endpoints and query URLs.
  function chatCompletionsURL(endpoint) {
    var raw = String(endpoint || "").trim();
    if (!raw || raw.indexOf("?") !== -1) return raw;

    var base = raw.replace(/\/+$/, "");
    var path = base.replace(/^[a-z][a-z0-9+.-]*:\/\/[^/]*/i, "");
    var resource = "/chat/completions";
    if (!path) return base + "/v1" + resource;

    var lastSegment = path.split("/").pop();
    if (lastSegment === "openai" || /^v\d+[a-z0-9]*$/i.test(lastSegment)) {
      return base + resource;
    }
    return raw;
  }

  // BabelDOC's OpenAI client appends /chat/completions to this base URL.
  // Keep query text here for diagnostics/tests; translation rejects it because BabelDOC
  // cannot preserve query parameters when it builds its own request URLs.
  function openAIBaseURLForBabelDOC(normalizedEndpoint) {
    var endpoint = String(normalizedEndpoint || "").trim();
    var queryIndex = endpoint.indexOf("?");
    var path = queryIndex === -1 ? endpoint : endpoint.slice(0, queryIndex);
    var query = queryIndex === -1 ? "" : endpoint.slice(queryIndex);
    path = path.replace(/\/+$/, "");
    return path.replace(/\/chat\/completions$/i, "") + query;
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
