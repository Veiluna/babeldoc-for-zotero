const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// The runner loads every helper by path from the Zotero data directory, so the
// plugin must copy each helper next to the runner. A missing entry only shows up
// as a silent "helper is missing" warning in production, so pin the contract here.

const runnerSource = fs.readFileSync(
  path.join(__dirname, "../addon/content/modules/runner.js"),
  "utf8"
);
const pythonDirectory = path.join(__dirname, "../addon/content/python");
const runnerFile = "ztf_babeldoc_runner.py";

const helperFiles = fs
  .readdirSync(pythonDirectory)
  .filter(name => name.endsWith(".py") && name !== runnerFile)
  .sort();

assert.ok(helperFiles.length >= 6, "expected the python helper modules on disk");

const missing = helperFiles.filter(
  name => runnerSource.indexOf('["' + name + '"') === -1
);
assert.deepEqual(
  missing,
  [],
  "runner.js must copy every python helper next to the runner: " + missing.join(", ")
);

for (const name of helperFiles) {
  assert.ok(
    fs.statSync(path.join(pythonDirectory, name)).isFile(),
    name + " must exist"
  );
}

// Every helper module loaded by the runner has to be in that copy list too.
const runnerPythonSource = fs.readFileSync(
  path.join(pythonDirectory, runnerFile),
  "utf8"
);
const loadedModules = Array.from(
  runnerPythonSource.matchAll(/_load_helper\("([a-z0-9_]+)"\)/g),
  match => match[1] + ".py"
);
assert.ok(loadedModules.length >= 6, "runner should load the helper modules by name");
for (const name of loadedModules) {
  assert.ok(
    helperFiles.includes(name),
    "runner loads " + name + " but it is not a helper file"
  );
}

console.log("python helper manifest regression cases passed");
