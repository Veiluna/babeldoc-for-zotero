const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../addon/content/modules/config.js"),
  "utf8"
);

const apiKey = "DEMO/SECRET-ONLY";
const preferences = {
  "extensions.babeldoc-for-zotero.baseURL": "https://example.test/v1",
  "extensions.babeldoc-for-zotero.apiKey": apiKey,
  "extensions.babeldoc-for-zotero.model": "test-model",
  "extensions.babeldoc-for-zotero.babeldocPath": "C:\\babeldoc\\python.exe",
  "extensions.babeldoc-for-zotero.maxOutputTokens": 8192,
  "extensions.babeldoc-for-zotero.maxRequests": 20,
};
const context = {
  Zotero: {
    Prefs: {
      get(name) { return preferences[name]; },
    },
  },
};

vm.runInNewContext(source, context, { filename: "config.js" });

const settings = context.ZoteroTranslateFilesConfig.getSettings();
assert.equal(settings.maxOutputTokens, 8192);
assert.equal(settings.maxRequests, 20);
preferences["extensions.babeldoc-for-zotero.maxOutputTokens"] = 1023;
assert.throws(
  () => context.ZoteroTranslateFilesConfig.getSettings(),
  /模型单次最大输出 token 数必须是大于或等于 1024 的整数/,
);
preferences["extensions.babeldoc-for-zotero.maxOutputTokens"] = 4096;
preferences["extensions.babeldoc-for-zotero.maxRequests"] = 0;
assert.throws(
  () => context.ZoteroTranslateFilesConfig.getSettings(),
  /单篇最多请求次数必须是大于或等于 1 的整数/,
);
assert.equal(
  context.ZoteroTranslateFilesConfig.redact("invalid setting: " + apiKey),
  "invalid setting: [已隐藏]",
  "redaction must read the configured API key even when another preference is invalid",
);
preferences["extensions.babeldoc-for-zotero.maxRequests"] = 20;

const message = context.ZoteroTranslateFilesConfig.redact(
  "invalid key " + apiKey + "; Authorization: Bearer bearer-secret-value; sk-1234567890"
);

for (const secret of [apiKey, "bearer-secret-value", "sk-1234567890"]) {
  assert.ok(!message.includes(secret), "redaction must remove configured keys and token-shaped secrets");
}
assert.ok(message.includes("[已隐藏]"));
assert.ok(message.includes("Bearer [已隐藏]"));
assert.ok(message.includes("[已隐藏 API key]"));
assert.equal(
  context.ZoteroTranslateFilesConfig.redact("Bearer scoped-token", "different-key"),
  "Bearer [已隐藏]",
  "the shared helper should also redact token-shaped diagnostics with an explicit API key",
);

const boundaryCases = [
  ["scope_Bearer bearer-shaped-token", "bearer-shaped-token"],
  ["prefix_sk-1234567890", "sk-1234567890"],
  ["prefix_sk-1234567- suffix", "sk-1234567-"],
  ["sk---------", "sk---------"],
];
for (const [input, secret] of boundaryCases) {
  const redacted = context.ZoteroTranslateFilesConfig.redact(input, "different-key");
  assert.ok(!redacted.includes(secret), `redaction must remove boundary case: ${input}`);
}

process.stdout.write("config redaction regression cases passed\n");
