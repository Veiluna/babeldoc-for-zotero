const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../addon/content/modules/config.js"),
  "utf8"
);
const context = { Zotero: { Prefs: { get: () => "" } } };
vm.runInNewContext(source, context, { filename: "config.js" });

const config = context.ZoteroTranslateFilesConfig;
const cases = [
  [
    "https://api.chatanywhere.tech",
    "https://api.chatanywhere.tech/v1/chat/completions",
    "https://api.chatanywhere.tech/v1",
  ],
  [
    "https://api.chatanywhere.tech/",
    "https://api.chatanywhere.tech/v1/chat/completions",
    "https://api.chatanywhere.tech/v1",
  ],
  [
    "https://api.chatanywhere.tech/v1",
    "https://api.chatanywhere.tech/v1/chat/completions",
    "https://api.chatanywhere.tech/v1",
  ],
  [
    "https://api.chatanywhere.tech/v1/",
    "https://api.chatanywhere.tech/v1/chat/completions",
    "https://api.chatanywhere.tech/v1",
  ],
  [
    "https://api.chatanywhere.tech/v1/chat/completions",
    "https://api.chatanywhere.tech/v1/chat/completions",
    "https://api.chatanywhere.tech/v1",
  ],
  [
    "https://api.chatanywhere.tech/v1/chat/completions/",
    "https://api.chatanywhere.tech/v1/chat/completions/",
    "https://api.chatanywhere.tech/v1",
  ],
  [
    "https://api.chatanywhere.tech/v1beta/openai",
    "https://api.chatanywhere.tech/v1beta/openai/chat/completions",
    "https://api.chatanywhere.tech/v1beta/openai",
  ],
  [
    "https://api.chatanywhere.tech/v1/chat/completions?tenant=demo",
    "https://api.chatanywhere.tech/v1/chat/completions?tenant=demo",
    "https://api.chatanywhere.tech/v1?tenant=demo",
  ],
  [
    "https://api.chatanywhere.tech/v1?tenant=demo",
    "https://api.chatanywhere.tech/v1?tenant=demo",
    "https://api.chatanywhere.tech/v1?tenant=demo",
  ],
  [
    "https://api.chatanywhere.tech/custom/endpoint",
    "https://api.chatanywhere.tech/custom/endpoint",
    "https://api.chatanywhere.tech/custom/endpoint",
  ],
];

for (const [input, normalizedEndpoint, babeldocBaseURL] of cases) {
  const normalized = config.chatCompletionsURL(input);
  assert.equal(normalized, normalizedEndpoint, `normalization failed for ${input}`);
  assert.equal(
    config.openAIBaseURLForBabelDOC(normalized),
    babeldocBaseURL,
    `BabelDOC base derivation failed for ${input}`,
  );
}

process.stdout.write("endpoint normalization regression cases passed\n");
