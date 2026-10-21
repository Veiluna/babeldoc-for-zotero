const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const repositoryRoot = path.resolve(__dirname, "..");
const addonPath = path.join(repositoryRoot, "addon");
const manifestPath = path.join(addonPath, "manifest.json");
const manifestBytes = fs.readFileSync(manifestPath);

assert.notDeepEqual(
  manifestBytes.subarray(0, 3),
  Buffer.from([0xef, 0xbb, 0xbf]),
  "manifest.json must not include a UTF-8 BOM"
);

const manifest = JSON.parse(manifestBytes.toString("utf8"));
assert.equal(manifest.manifest_version, 2);
assert.equal(manifest.name, "BabelDOC for Zotero");
assert.match(manifest.description, /Translate PDF attachments/);
assert.equal(manifest.version, "0.5.2");

const zotero = manifest.applications && manifest.applications.zotero;
assert.ok(zotero, "applications.zotero is required");
assert.equal(zotero.id, "babeldoc-for-zotero@veiluna.github.io");
assert.equal(
  zotero.update_url,
  "https://github.com/Veiluna/babeldoc-for-zotero/releases/download/release/updates.json"
);
assert.equal(new URL(zotero.update_url).protocol, "https:");
assert.equal(zotero.strict_min_version, "6.999");
assert.equal(typeof zotero.strict_min_version, "string");
assert.ok(!zotero.strict_min_version.includes("*"), "strict_min_version cannot contain a wildcard");
assert.equal(zotero.strict_max_version, "10.*");
assert.equal(typeof zotero.strict_max_version, "string");
assert.ok(zotero.strict_max_version.length > 0, "strict_max_version is required");

// Reproduce Mozilla's version-part format:
// <number-a><string-b><number-c><string-d>, with absent numbers equal to 0.
function parseVersionPart(part) {
  if (part === "*") return { infinity: true };

  let cursor = 0;
  function readNumber() {
    const match = /^-?\d+/.exec(part.slice(cursor));
    if (!match) return 0;
    cursor += match[0].length;
    return Number(match[0]);
  }
  function beginsNumberAt(index) {
    return /\d/.test(part[index] || "") ||
      (part[index] === "-" && /\d/.test(part[index + 1] || ""));
  }

  let numberA = readNumber();
  const stringBStart = cursor;
  while (cursor < part.length && !beginsNumberAt(cursor)) cursor++;
  let stringB = part.slice(stringBStart, cursor);
  const numberC = readNumber();
  const stringD = part.slice(cursor);

  // Legacy Firefox versions use 1.0+ as an alias for 1.1pre.
  if (stringB === "+") {
    numberA++;
    stringB = "pre";
  }

  return { infinity: false, numberA, stringB, numberC, stringD };
}

function compareNumbers(left, right) {
  return left === right ? 0 : left < right ? -1 : 1;
}

function compareStrings(left, right) {
  // In nsIVersionComparator, a present string sorts before an absent string;
  // when both exist, compare their bytes (version strings are ASCII here).
  if (!left || !right) {
    if (Boolean(left) === Boolean(right)) return 0;
    return left ? -1 : 1;
  }
  return Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8"));
}

function compareVersionParts(leftPart, rightPart) {
  const left = parseVersionPart(leftPart);
  const right = parseVersionPart(rightPart);

  if (left.infinity || right.infinity) {
    if (left.infinity === right.infinity) return 0;
    return left.infinity ? 1 : -1;
  }

  return compareNumbers(left.numberA, right.numberA) ||
    compareStrings(left.stringB, right.stringB) ||
    compareNumbers(left.numberC, right.numberC) ||
    compareStrings(left.stringD, right.stringD);
}

function compareVersions(leftVersion, rightVersion) {
  const leftParts = String(leftVersion).split(".");
  const rightParts = String(rightVersion).split(".");
  const partCount = Math.max(leftParts.length, rightParts.length);

  for (let i = 0; i < partCount; i++) {
    const comparison = compareVersionParts(leftParts[i] || "", rightParts[i] || "");
    if (comparison !== 0) return comparison;
  }

  return 0;
}

assert.ok(compareVersions("10.0.6", zotero.strict_min_version) >= 0);
assert.ok(compareVersions("10.0.6", zotero.strict_max_version) <= 0);
assert.equal(compareVersions("10.0.6", "10.*"), -1, "wildcard component covers later 10.x parts");
assert.equal(compareVersions("11.0", "10.*"), 1);
assert.equal(compareVersions("10.0", "10.0.0"), 0, "missing numeric parts compare as zero");
assert.equal(compareVersions("10.0pre1", "10.0a1"), 1, "string-b suffixes compare bytewise");
assert.equal(compareVersions("10.0+", "10.1pre"), 0, "plus normalizes to the next pre-release");
assert.equal(compareVersions("1.0+", "1.1pre"), 0);
assert.equal(compareVersions("10.0a1", "10.0b1"), -1);
assert.equal(compareVersions("10.0b1", "10.0"), -1);
assert.equal(compareVersions("1.1pre2", "1.1pre10"), -1, "number-c parts compare numerically");
assert.equal(compareVersions("1.1pre1a", "1.1pre1"), -1, "a present string-d sorts first");
assert.equal(compareVersions("1.1pre1a", "1.1pre1b"), -1, "string-d parts compare bytewise");
assert.equal(compareVersions("1.1.-1", "1.1"), -1, "negative numeric parts are supported");
assert.equal(compareVersions("1.0..", "1.0"), 0, "empty and missing parts compare as zero");

const updatesPath = path.join(repositoryRoot, "updates.json");
const updatesBytes = fs.readFileSync(updatesPath);
assert.notDeepEqual(
  updatesBytes.subarray(0, 3),
  Buffer.from([0xef, 0xbb, 0xbf]),
  "updates.json must not include a UTF-8 BOM"
);
const updatesManifest = JSON.parse(updatesBytes.toString("utf8"));
const release = updatesManifest.addons[zotero.id].updates[0];
assert.equal(release.version, manifest.version, "updates.json must match the packaged version");
assert.equal(
  release.update_link,
  "https://github.com/Veiluna/babeldoc-for-zotero/releases/download/v0.5.2/babeldoc-for-zotero-0.5.2.xpi"
);
assert.equal(release.applications.zotero.strict_min_version, zotero.strict_min_version);
assert.equal(release.applications.zotero.strict_max_version, zotero.strict_max_version);

function walkPackageFiles(directory) {
  const files = [];
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...walkPackageFiles(fullPath));
    else if (entry.isFile()) files.push(path.relative(addonPath, fullPath).split(path.sep).join("/"));
  }
  return files;
}

function readZipEntryNames(xpiPath) {
  const bytes = fs.readFileSync(xpiPath);
  const endSignature = Buffer.from([0x50, 0x4b, 0x05, 0x06]);
  const minimumOffset = Math.max(0, bytes.length - 0xffff - 22);
  let endOffset = -1;
  for (let offset = bytes.length - 22; offset >= minimumOffset; offset--) {
    if (bytes.subarray(offset, offset + 4).equals(endSignature)) {
      endOffset = offset;
      break;
    }
  }
  assert.notEqual(endOffset, -1, `${path.basename(xpiPath)} must be a valid ZIP archive`);

  const entryCount = bytes.readUInt16LE(endOffset + 10);
  let centralOffset = bytes.readUInt32LE(endOffset + 16);
  const names = [];
  for (let i = 0; i < entryCount; i++) {
    assert.equal(bytes.readUInt32LE(centralOffset), 0x02014b50, "invalid ZIP central directory entry");
    const nameLength = bytes.readUInt16LE(centralOffset + 28);
    const extraLength = bytes.readUInt16LE(centralOffset + 30);
    const commentLength = bytes.readUInt16LE(centralOffset + 32);
    names.push(bytes.toString("utf8", centralOffset + 46, centralOffset + 46 + nameLength));
    centralOffset += 46 + nameLength + extraLength + commentLength;
  }
  return names;
}

const expectedXpiPath = path.join(
  repositoryRoot,
  "build",
  `babeldoc-for-zotero-${manifest.version}.xpi`
);
let packageEntries;
if (fs.existsSync(expectedXpiPath)) {
  packageEntries = readZipEntryNames(expectedXpiPath);
} else {
  // Mirror the builder's addon/ -> XPI-root path mapping when no build artifact exists yet.
  packageEntries = walkPackageFiles(addonPath);
}
assert.ok(packageEntries.includes("manifest.json"), "XPI must contain manifest.json at its root");
assert.ok(packageEntries.includes("bootstrap.js"), "XPI must contain bootstrap.js at its root");
assert.ok(packageEntries.includes("content/icons/icon.svg"), "manifest icon path must exist in the package");

const builtUpdatesPath = path.join(repositoryRoot, "build", "updates.json");
if (fs.existsSync(builtUpdatesPath)) {
  const builtUpdates = JSON.parse(fs.readFileSync(builtUpdatesPath, "utf8"));
  const builtRelease = builtUpdates.addons[zotero.id].updates[0];
  assert.equal(builtRelease.version, manifest.version);
  assert.equal(
    builtRelease.update_link,
    `https://github.com/Veiluna/babeldoc-for-zotero/releases/download/v${manifest.version}/babeldoc-for-zotero-${manifest.version}.xpi`
  );
  assert.equal(builtRelease.applications.zotero.strict_min_version, zotero.strict_min_version);
  assert.equal(builtRelease.applications.zotero.strict_max_version, zotero.strict_max_version);
}

console.log("Zotero 10 add-on manifest and package checks passed.");
