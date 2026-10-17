const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const repositoryRoot = path.resolve(__dirname, "..");
const addonRoot = path.join(repositoryRoot, "addon");
const read = relativePath => fs.readFileSync(path.join(repositoryRoot, relativePath), "utf8");

const manifest = JSON.parse(read("addon/manifest.json"));
assert.equal(manifest.name, "BabelDOC for Zotero");
assert.equal(manifest.applications.zotero.id, "babeldoc-for-zotero@veiluna.github.io");
assert.equal(
  manifest.applications.zotero.update_url,
  "https://github.com/Veiluna/babeldoc-for-zotero/releases/download/release/updates.json",
);

for (const locale of ["zh-CN", "en-US"]) {
  const fileName = "babeldoc-for-zotero.ftl";
  const filePath = path.join(addonRoot, "locale", locale, fileName);
  assert.ok(fs.existsSync(filePath), `${locale} localization file must use the new plugin name`);
  const localization = fs.readFileSync(filePath, "utf8");
  assert.match(localization, /^babeldoc-for-zotero-name = BabelDOC for Zotero$/m);
  assert.match(localization, /^babeldoc-for-zotero-pref-pane-label = BabelDOC for Zotero$/m);
  assert.doesNotMatch(localization, /^zotero-translate-files-/m);
  assert.ok(
    !fs.existsSync(path.join(addonRoot, "locale", locale, "zotero-translate-files.ftl")),
    `${locale} must not package the old localization file`,
  );
}

for (const relativePath of [
  "addon/bootstrap.js",
  "addon/content/preferences.js",
  "addon/content/modules/queue.js",
  "addon/content/modules/importer.js",
]) {
  const source = read(relativePath);
  assert.match(source, /babeldoc-for-zotero\.ftl/);
  assert.doesNotMatch(source, /zotero-translate-files\.ftl/);
}

const preferenceMarkup = read("addon/content/preferences.xhtml");
assert.match(preferenceMarkup, /id="babeldoc-for-zotero-preferences"/);
assert.match(preferenceMarkup, /preference="extensions\.babeldoc-for-zotero\.baseURL"/);
assert.doesNotMatch(preferenceMarkup, /(?:id|class)="zotero-translate-files-/);
assert.match(read("addon/content/modules/menu.js"), /var MENU_ID = "babeldoc-for-zotero-translate-pdf"/);

const prefsSource = read("addon/prefs.js");
assert.match(prefsSource, /extensions\.babeldoc-for-zotero\.baseURL/);
assert.doesNotMatch(prefsSource, /extensions\.zotero-translate-files\./);

for (const relativePath of [
  "addon/bootstrap.js",
  "addon/content/main.js",
  "addon/content/modules/queue.js",
]) {
  for (const line of read(relativePath).split(/\r?\n/).filter(line => line.includes("Zotero.debug("))) {
    assert.match(line, /BabelDOC for Zotero: /, `${relativePath} must use the current debug prefix`);
  }
}

assert.match(read("addon/content/preferences.js"), /return "BabelDOC for Zotero"/);
assert.match(read("addon/content/modules/queue.js"), /progress\.changeHeadline\("BabelDOC for Zotero"\)/);
assert.equal(manifest.version, "0.4.0");
assert.match(read("addon/bootstrap.js"), /content\/modules\/installer\.js/);
assert.match(read("addon/content/preferences.xhtml"), /babeldoc-for-zotero\.downloadSource/);
assert.match(read("addon/content/preferences.xhtml"), /babeldoc-for-zotero-pref-section-environment/);

const readme = read("README.md");
assert.match(readme, /^# BabelDOC for Zotero$/m);
assert.match(readme, /设置会自动迁移；旧版 Translate Files for Zotero 需手动卸载/);
assert.match(readme, /build\\babeldoc-for-zotero-<版本>\.xpi/);
assert.match(read("addon/content/modules/installer.js"), /UV_VERSION = "0\.13\.1"/);
assert.match(read("README.md"), /首次翻译时，插件会在 `%LOCALAPPDATA%\\babeldoc-for-zotero`/);

process.stdout.write("BabelDOC for Zotero branding regression cases passed\n");
