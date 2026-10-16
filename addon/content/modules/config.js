/* global Zotero */

// Shared by the preferences pane and the translation runner.
var ZoteroTranslateFilesConfig = (function () {
  var preferences = {
    baseURL: "extensions.zotero-translate-files.baseURL",
    apiKey: "extensions.zotero-translate-files.apiKey",
    model: "extensions.zotero-translate-files.model",
    babeldocPath: "extensions.zotero-translate-files.babeldocPath",
    maxOutputTokens: "extensions.zotero-translate-files.maxOutputTokens",
    maxRequests: "extensions.zotero-translate-files.maxRequests",
  };

  function getSettings() {
    function read(name) {
      var value = Zotero.Prefs.get(preferences[name], true);
      return String(value == null ? "" : value).trim();
    }

    function readInteger(name, fallback, minimum, label) {
      var value = Zotero.Prefs.get(preferences[name], true);
      if (value == null || String(value).trim() === "") return fallback;
      var number = Number(value);
      if (!Number.isSafeInteger(number) || number < minimum) {
        throw new Error(label + "必须是大于或等于 " + minimum + " 的整数。");
      }
      return number;
    }

    return {
      baseURL: read("baseURL"),
      apiKey: read("apiKey"),
      model: read("model"),
      babeldocPath: read("babeldocPath"),
      maxOutputTokens: readInteger(
        "maxOutputTokens",
        8192,
        1024,
        "模型单次最大输出 token 数"
      ),
      maxRequests: readInteger("maxRequests", 20, 1, "单篇最多请求次数"),
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
        // Redaction must remain available even if an unrelated setting is invalid.
        configuredKey = Zotero.Prefs.get(preferences.apiKey, true);
        configuredKey = String(configuredKey == null ? "" : configuredKey).trim();
      } catch (error) {
        configuredKey = "";
      }
    }
    if (configuredKey) text = text.split(String(configuredKey)).join("[已隐藏]");

    return text
      .replace(/Bearer\s+\S+/gi, "Bearer [已隐藏]")
      .replace(/sk-[A-Za-z0-9_-]{8,}/g, "[已隐藏 API key]");
  }

  function decodeHTMLEntities(value) {
    return String(value || "").replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos);/gi, function (entity, code) {
      var normalized = code.toLowerCase();
      if (normalized === "amp") return "&";
      if (normalized === "lt") return "<";
      if (normalized === "gt") return ">";
      if (normalized === "quot") return '"';
      if (normalized === "apos") return "'";
      var number = normalized.indexOf("#x") === 0
        ? parseInt(normalized.slice(2), 16)
        : parseInt(normalized.slice(1), 10);
      try {
        return Number.isFinite(number) ? String.fromCodePoint(number) : entity;
      } catch (error) {
        return entity;
      }
    });
  }

  // API providers sometimes send a branded HTML error page instead of JSON.
  // Keep only its status, title, and request ID for diagnostic display.
  function formatHTMLDiagnostic(value) {
    var text;
    try {
      text = String(value == null ? "" : value);
    } catch (error) {
      return "无法读取错误详情";
    }
    if (!/<!doctype\s+html|<html\b|<script\b|<title\b/i.test(text)) return text;

    var statusMatch = text.match(/\b(?:HTTP(?:\/\d(?:\.\d)?)?\s+|Error code:\s*|status(?:_code)?\s*[=: ]\s*)(\d{3})\b/i);
    var titleMatch = text.match(/<title\b[^>]*>([\s\S]*?)<\/title\s*>/i);
    var title = titleMatch
      ? decodeHTMLEntities(titleMatch[1].replace(/<[^>]*>/g, " ")).replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim()
      : "";

    var requestID = "";
    var elementMatch = text.match(/<[^>]*\bid\s*=\s*(['"]?)requestId\1[^>]*>([\s\S]*?)<\/[^>]+>/i);
    if (elementMatch) {
      requestID = decodeHTMLEntities(elementMatch[2].replace(/<[^>]*>/g, " ")).replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
    }
    if (!requestID) {
      var requestMatch = text.match(/\brequest[_ -]?id\s*[:=]\s*['"]?([A-Za-z0-9._-]{5,})/i);
      if (!requestMatch) {
        requestMatch = text.match(/requestId[\s\S]{0,250}?\b[A-Za-z_$][\w$]*\s*\(\s*['"]([A-Za-z0-9._-]{5,})['"]\s*\)/i);
      }
      if (requestMatch) requestID = requestMatch[1];
    }

    var parts = [];
    if (statusMatch) parts.push("HTTP " + statusMatch[1]);
    parts.push(title || "接口返回了网页错误");
    if (requestID) parts.push("requestId: " + requestID);
    if (statusMatch) return parts[0] + "：" + parts[1] + (parts[2] ? "（" + parts[2] + "）" : "");
    return parts.join("；");
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
    formatHTMLDiagnostic: formatHTMLDiagnostic,
    chatCompletionsURL: chatCompletionsURL,
    openAIBaseURLForBabelDOC: openAIBaseURLForBabelDOC,
    resolvePythonFromBabelDOCPath: resolvePythonFromBabelDOCPath,
  };
})();
