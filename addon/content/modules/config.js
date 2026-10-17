/* global Zotero */

// Shared by the preferences pane and the translation runner.
var ZoteroTranslateFilesConfig = (function () {
  var PREFERENCE_PREFIX = "extensions.babeldoc-for-zotero.";
  var LEGACY_PREFERENCE_PREFIX = "extensions.zotero-translate-files.";
  var LEGACY_MIGRATION_PREFERENCE = PREFERENCE_PREFIX + "legacyPrefsMigrated";

  var preferences = {
    baseURL: PREFERENCE_PREFIX + "baseURL",
    apiKey: PREFERENCE_PREFIX + "apiKey",
    model: PREFERENCE_PREFIX + "model",
    downloadSource: PREFERENCE_PREFIX + "downloadSource",
    maxOutputTokens: PREFERENCE_PREFIX + "maxOutputTokens",
    maxRequests: PREFERENCE_PREFIX + "maxRequests",
    repairFailed: PREFERENCE_PREFIX + "repairFailed",
    translateReferences: PREFERENCE_PREFIX + "translateReferences",
    // Learned, not user-facing: "max_tokens" or "max_completion_tokens".
    tokenParam: PREFERENCE_PREFIX + "tokenParam",
  };

  function migrateLegacyPreferences() {
    if (Zotero.Prefs.get(LEGACY_MIGRATION_PREFERENCE, true) === true) return 0;

    var migrated = 0;
    Object.keys(preferences).forEach(function (name) {
      var currentPreference = preferences[name];
      var legacyPreference = LEGACY_PREFERENCE_PREFIX + name;
      if (!Zotero.Prefs.prefHasUserValue(currentPreference, true) &&
          Zotero.Prefs.prefHasUserValue(legacyPreference, true)) {
        Zotero.Prefs.set(
          currentPreference,
          Zotero.Prefs.get(legacyPreference, true),
          true
        );
        migrated++;
      }
    });

    Zotero.Prefs.set(LEGACY_MIGRATION_PREFERENCE, true, true);
    if (Zotero.debug) {
      Zotero.debug("BabelDOC for Zotero: migrated " + migrated + " legacy preferences");
    }
    return migrated;
  }

  var TOKEN_LIMIT_PARAMETERS = ["max_tokens", "max_completion_tokens"];
  var ADAPTABLE_PARAMETERS = TOKEN_LIMIT_PARAMETERS;

  function rejectedParameter(message) {
    // Match the parameter named by the rejection itself. A provider may also
    // mention the replacement in the same message ("Use X instead"), which
    // must not be mistaken for the rejected parameter.
    var patterns = [
      /\bunsupported\s+parameter\s*:\s*["'`]?([a-z_]+)["'`]?/i,
      /["'`]?([a-z_]+)["'`]?\s+(?:parameter\s+)?(?:is\s+)?not\s+supported\b/i,
      /\bdoes\s+not\s+support\s+(?:the\s+)?(?:parameter\s+)?["'`]?([a-z_]+)["'`]?/i,
    ];
    for (var i = 0; i < patterns.length; i++) {
      var match = patterns[i].exec(message);
      if (match && ADAPTABLE_PARAMETERS.indexOf(match[1]) !== -1) return match[1];
    }
    return "";
  }

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

    function readBoolean(name, fallback) {
      var value = Zotero.Prefs.get(preferences[name], true);
      if (value == null) return fallback;
      if (typeof value === "boolean") return value;
      var normalized = String(value).trim().toLowerCase();
      if (["true", "1", "yes", "on"].indexOf(normalized) !== -1) return true;
      if (["false", "0", "no", "off"].indexOf(normalized) !== -1) return false;
      throw new Error(name + " 必须是布尔值。");
    }

    return {
      baseURL: read("baseURL"),
      apiKey: read("apiKey"),
      model: read("model"),
      downloadSource: getDownloadSource(),
      maxOutputTokens: readInteger(
        "maxOutputTokens",
        8192,
        1024,
        "模型单次最大输出 token 数"
      ),
      maxRequests: readInteger("maxRequests", 20, 1, "单篇最多请求次数"),
      repairFailed: readBoolean("repairFailed", true),
      translateReferences: readBoolean("translateReferences", false),
      tokenParam: TOKEN_LIMIT_PARAMETERS.indexOf(read("tokenParam")) !== -1
        ? read("tokenParam")
        : "max_tokens",
    };
  }

  function getDownloadSource() {
    var value = Zotero.Prefs.get(preferences.downloadSource, true);
    return String(value == null ? "" : value).trim() === "official" ? "official" : "china";
  }

  function setTokenParam(value) {
    if (TOKEN_LIMIT_PARAMETERS.indexOf(value) === -1) return false;
    Zotero.Prefs.set(preferences.tokenParam, value, true);
    return true;
  }

  // Some models reject max_tokens ("Use 'max_completion_tokens' instead") and
  // others the reverse. Such a 400 is a validation error that costs no quota.
  // Returns the parameter name to use instead, or "" for any other response.
  function tokenLimitAdaptation(status, responseText) {
    if (Number(status) !== 400) return "";
    var details = {};
    try {
      var body = JSON.parse(String(responseText || ""));
      details = (body && typeof body.error === "object" && body.error) || body || {};
    } catch (error) {
      details = {};
    }
    var param = String(details.param || "");
    var message = (String(details.message || "") + " " + String(responseText || "")).toLowerCase();
    var unsupported = /unsupported|not supported/.test(String(details.code || "") + " " + message);
    if (!unsupported) return "";
    // Structured error fields are authoritative; only inspect message text
    // when the provider omitted `param`.
    if (param === "max_tokens") {
      return "max_completion_tokens";
    }
    if (param === "max_completion_tokens") {
      return "max_tokens";
    }
    var rejected = rejectedParameter(message);
    if (rejected === "max_tokens") return "max_completion_tokens";
    if (rejected === "max_completion_tokens") return "max_tokens";
    return "";
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

  return {
    migrateLegacyPreferences: migrateLegacyPreferences,
    getSettings: getSettings,
    getDownloadSource: getDownloadSource,
    setTokenParam: setTokenParam,
    tokenLimitAdaptation: tokenLimitAdaptation,
    redact: redact,
    formatHTMLDiagnostic: formatHTMLDiagnostic,
    chatCompletionsURL: chatCompletionsURL,
    openAIBaseURLForBabelDOC: openAIBaseURLForBabelDOC,
  };
})();
