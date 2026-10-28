const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// 0.8.0 已删除 4 个历史表格 helper，既不复制到 Zotero 数据目录，
// 也不再被 runner 的 _load_helper 加载。本测试
// 固定“实际部署 helper 清单 = runner.js pythonHelpers = runner._load_helper”。
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
const expectedHelpers = [
  "ztf_figure_text.py",
  "ztf_inline_formulas.py",
  "ztf_links.py",
  "ztf_punctuation_cleanup.py",
  "ztf_split_columns.py",
  "ztf_split_compose.py",
  "ztf_split_lines.py",
  "ztf_split_margins.py",
  "ztf_split_plan.py",
  "ztf_split_types.py",
  "ztf_split_variants.py",
  "ztf_style_guards.py",
].sort();
assert.deepEqual(
  helperFiles,
  expectedHelpers,
  "部署清单只包含保留的 Python helper（含 0.8.0 分栏模块），已删除的表格模块不得部署"
);
assert.ok(helperFiles.length >= 5, "expected the remaining python helper modules on disk");

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

// 已删除的表格模块既不部署也不由 runner 加载。
const archivedHelpers = [
  "ztf_table_text.py",
  "ztf_table_headers.py",
  "ztf_table2.py",
  "ztf_table_layout.py",
];
for (const name of archivedHelpers) {
  assert.ok(!helperFiles.includes(name), name + " 已封存，不应出现在部署目录");
  assert.equal(
    runnerSource.indexOf('["' + name + '"'),
    -1,
    name + " 已封存，runner.js 不得复制"
  );
}

const runnerPythonSource = fs.readFileSync(
  path.join(pythonDirectory, runnerFile),
  "utf8"
);
const loadedModules = Array.from(
  runnerPythonSource.matchAll(/_load_helper\("([a-z0-9_]+)"\)/g),
  match => match[1] + ".py"
).sort();
assert.deepEqual(
  loadedModules,
  [
    "ztf_figure_text.py",
    "ztf_links.py",
    "ztf_punctuation_cleanup.py",
    "ztf_split_compose.py",
    "ztf_split_plan.py",
    "ztf_split_variants.py",
    "ztf_style_guards.py",
  ],
  "runner 只能加载未封存的 helper 模块"
);
for (const name of loadedModules) {
  assert.ok(
    helperFiles.includes(name),
    "runner loads " + name + " but it is not a deployed helper file"
  );
}
for (const name of archivedHelpers) {
  const moduleName = name.replace(/\.py$/, "");
  assert.equal(
    runnerPythonSource.indexOf(moduleName),
    -1,
    name + " 已封存，runner 源码不得再引用 " + moduleName
  );
}

console.log("python helper manifest regression cases passed");
