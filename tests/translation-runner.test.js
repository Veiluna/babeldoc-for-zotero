const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const runnerSource = fs.readFileSync(
  path.join(__dirname, "../addon/content/modules/runner.js"),
  "utf8"
);
const configSource = fs.readFileSync(
  path.join(__dirname, "../addon/content/modules/config.js"),
  "utf8"
);

function deferred() {
  let resolve;
  const promise = new Promise(resolvePromise => { resolve = resolvePromise; });
  return { promise, resolve };
}

function makeStream(text) {
  const bytes = Buffer.from(text, "utf8");
  const chunks = Array.from(bytes, byte => Uint8Array.of(byte));
  let byteIndex = 0;
  let stringIndex = 0;
  const legacyDecoder = new TextDecoder("utf-8");

  return {
    async read() {
      return byteIndex < chunks.length ? chunks[byteIndex++] : new Uint8Array(0);
    },
    // Model readString() returning an empty string for an incomplete UTF-8 character.
    async readString() {
      if (stringIndex >= chunks.length) return "";
      return legacyDecoder.decode(chunks[stringIndex++], { stream: true });
    },
  };
}

function makeHarness(options = {}) {
  const apiKey = options.apiKey || "RUNNER_SECRET_KEY_789";
  const inputPath = "C:\\Library\\paper.pdf";
  const pythonPath = "C:\\babeldoc\\python.exe";
  const files = new Map([
    [inputPath, { exists: true, isFile: true, fileSize: 500 }],
    [pythonPath, { exists: true, isFile: true, fileSize: 1000 }],
  ]);
  const calls = [];
  const copiedFiles = [];
  const createdDirectories = [];
  const removedDirectories = [];
  const progress = [];
  const waitGate = deferred();
  let processStartedResolve;
  const processStarted = new Promise(resolve => { processStartedResolve = resolve; });
  let killCount = 0;

  const fileAPI = {
    pathToFile(filePath) {
      const entry = files.get(filePath);
      return {
        exists: () => !!(entry && entry.exists),
        isFile: () => !!(entry && entry.isFile),
        get fileSize() { return entry ? entry.fileSize : 0; },
      };
    },
    async getContentsFromURL(url) {
      copiedFiles.push(["read-resource", url]);
      return "# copied runner source";
    },
    async putContentsAsync(filePath, contents, charset) {
      copiedFiles.push(["write-runner", filePath, contents, charset]);
    },
    async createDirectoryIfMissingAsync(directory) {
      createdDirectories.push(directory);
    },
    async iterateDirectory(directory, callback) {
      if (typeof options.directoryEntries === "function") {
        options.directoryEntries(directory, callback, files);
      } else {
        for (const entry of options.directoryEntries || []) callback(entry);
      }
    },
  };

  const subprocess = {
    async call(callOptions) {
      calls.push(callOptions);
      processStartedResolve();
      const outputDirectory = callOptions.arguments[callOptions.arguments.indexOf("--output") + 1];
      const outputPath = outputDirectory + "\\paper.zh.mono.pdf";
      if (options.outputSize !== 0) files.set(outputPath, { exists: true, isFile: true, fileSize: 800 });
      const dualPath = outputDirectory + "\\paper.zh.dual.pdf";
      if (options.outputMode === "dual" || options.outputMode === "both") {
        files.set(dualPath, { exists: true, isFile: true, fileSize: 900 });
      }

      const progressLine = "ZTF_PROGRESS " + JSON.stringify(Object.assign({
        overall: 42.5,
        stage: "翻译段落",
        current: 3,
        total: 10,
      }, options.progressRequests || {})) + "\r\n";
      const resultLine = options.errorEvent
        ? "ZTF_ERROR " + JSON.stringify(options.errorEvent === true
          ? { error: "request failed with " + apiKey }
          : options.errorEvent) + "\r\n"
        : options.omitResult
          ? ""
          : "ZTF_RESULT " + JSON.stringify(Object.assign(
            { mono: outputPath },
            options.outputMode === "dual" || options.outputMode === "both"
              ? { dual: dualPath }
              : {},
            options.requestCount == null ? {} : { requests: options.requestCount },
            options.repairRequestCount == null ? {} : { repair_request_count: options.repairRequestCount },
            options.fragmentItemCount == null ? {} : { fragment_item_count: options.fragmentItemCount },
            options.logPath == null ? {} : { log_path: options.logPath },
            options.links == null ? {} : { links: options.links },
          )) + "\r\n";
      const warningLine = options.warning
        ? "ZTF_WARNING " + JSON.stringify(options.warning) + "\r\n"
        : "";
      const stdout = makeStream(progressLine + warningLine + resultLine);
      const stderr = makeStream(options.stderrText || (
        "diagnostic includes " + apiKey + "\r\n" +
        "scope_Bearer boundary-bearer-secret\r\n" +
        "prefix_sk-1234567890\r\n"
      ));

      return {
        stdin: { close() {} },
        stdout,
        stderr,
        kill() {
          killCount++;
          waitGate.resolve({ exitCode: -9 });
        },
        wait() {
          return options.waitForExit ? waitGate.promise : Promise.resolve({ exitCode: options.exitCode ?? 0 });
        },
      };
    },
  };

  const context = {
    ZoteroTranslateFilesInstaller: {
      async isReady() { return true; },
      async ensureReady() { return pythonPath; },
    },
    ChromeUtils: {
      importESModule(uri) {
        if (uri.endsWith("Subprocess.sys.mjs")) return { Subprocess: subprocess };
        throw new Error("Unexpected module: " + uri);
      },
    },
    IOUtils: {
      async makeDirectory(directory) { createdDirectories.push(directory); },
      async remove(directory) { removedDirectories.push(directory); },
      async copy(source, destination) { copiedFiles.push(["copy-input", source, destination]); },
    },
    PathUtils: {
      tempDir: "C:\\Temp",
      join: (...parts) => parts.join("\\"),
    },
    TextDecoder,
    Services: { uuid: { generateUUID: () => "{test-job-id}" } },
    Zotero: {
      Prefs: {
        get(name) {
          const prefix = "extensions.babeldoc-for-zotero.";
          const values = {
            baseURL: options.baseURL || "https://api.example.test/v1/chat/completions",
            apiKey,
            model: "test-model",
            outputMode: options.outputMode || "mono",
          };
          return values[name.slice(prefix.length)];
        },
      },
      File: fileAPI,
      DataDirectory: { dir: "C:\\Zotero" },
    },
    ZoteroTranslateFiles: { rootURI: "jar:file:///plugin.xpi!/" },
  };
  vm.runInNewContext(configSource, context, { filename: "config.js" });
  vm.runInNewContext(runnerSource, context, { filename: "runner.js" });

  return {
    runner: context.ZoteroTranslateFilesRunner,
    inputPath,
    apiKey,
    calls,
    copiedFiles,
    createdDirectories,
    removedDirectories,
    progress,
    files,
    processStarted,
    waitGate,
    get killCount() { return killCount; },
    item: {
      key: "ABCD1234",
      getFilePathAsync: async () => inputPath,
      getField: () => "Paper title",
    },
  };
}

async function copiesResourcesLaunchesSafelyAndParsesProgress() {
  const harness = makeHarness();
  const result = await harness.runner.translate(harness.item, {
    onProgress: value => harness.progress.push(value),
  });

  assert.match(harness.copiedFiles[0][1], /content\/python\/ztf_babeldoc_runner\.py$/);
  assert.ok(harness.copiedFiles.some(entry => entry[0] === "write-runner"));
  assert.equal(harness.copiedFiles.find(entry => entry[0] === "write-runner")[3], "utf-8");
  assert.ok(harness.copiedFiles.some(entry => entry[0] === "copy-input" && entry[2].endsWith("\\input.pdf")));
  const runnerDirectory = path.win32.dirname(harness.calls[0].arguments[0]);
  for (const helperName of [
    "ztf_inline_formulas.py", "ztf_style_guards.py",
    "ztf_links.py", "ztf_table_headers.py",
  ]) {
    assert.ok(harness.copiedFiles.some(entry => entry[0] === "read-resource" &&
      entry[1].endsWith("content/python/" + helperName)), "读取 Python 辅助模块 " + helperName);
    assert.ok(harness.copiedFiles.some(entry => entry[0] === "write-runner" &&
      entry[1] === path.win32.join(runnerDirectory, helperName) &&
      entry[2] && entry[3] === "utf-8"), "辅助模块与实际执行的 runner 放在同一目录 " + helperName);
  }
  assert.ok(harness.createdDirectories.includes("C:\\Zotero\\babeldoc-for-zotero"));
  assert.ok(harness.createdDirectories.includes("C:\\Zotero\\babeldoc-for-zotero\\logs"));
  assert.equal(harness.createdDirectories.length, 4, "data, logs, job, and BabelDOC working directories should be created");

  const call = harness.calls[0];
  const args = call.arguments;
  assert.equal(call.command, "C:\\babeldoc\\python.exe");
  assert.ok(args.includes("--no-dual"));
  assert.ok(args.includes("--no-auto-extract-glossary"));
  assert.ok(args.includes("--disable-same-text-fallback"));
  // The prompt is owned by the Python runner (ensure_runner_options); passing
  // a copy from JS previously replaced the runner's fragment rules.
  assert.equal(args.indexOf("--custom-system-prompt"), -1);
  assert.equal(call.environment.ZTF_MAX_OUTPUT_TOKENS, undefined);
  assert.equal(call.environment.ZTF_MAX_REQUESTS, undefined);
  assert.equal(call.environment.ZTF_REPAIR_FAILED, undefined);
  assert.equal(call.environment.ZTF_TRANSLATE_REFERENCES, "0");
  assert.equal(call.environment.ZTF_TOKEN_PARAM, "max_tokens");
  assert.match(call.environment.ZTF_JOB_LOG_PATH, /\\logs\\.*-ABCD1234\.jsonl$/);
  const workingDir = args[args.indexOf("--working-dir") + 1];
  assert.equal(workingDir, "C:\\Temp\\zotero-translate-files-test-job-id\\babeldoc-work");
  assert.ok(workingDir.startsWith("C:\\Temp\\zotero-translate-files-test-job-id\\"));
  assert.ok(args.includes("no_watermark"));
  assert.ok(args.includes("https://api.example.test/v1"));
  assert.ok(!args.includes(harness.apiKey), "the API key must never appear in process arguments");
  assert.equal(call.environment.ZTF_OPENAI_API_KEY, harness.apiKey);
  assert.equal(call.environmentAppend, true);
  assert.equal(call.stderr, "pipe");
  assert.equal(result.outputPath, "C:\\Temp\\zotero-translate-files-test-job-id\\paper.zh.mono.pdf");
  assert.equal(harness.progress.length, 1);
  assert.deepEqual(
    JSON.parse(JSON.stringify(harness.progress[0])),
    { overall: 42.5, stage: "翻译段落", current: 3, total: 10 },
  );

  await result.cleanup();
  assert.deepEqual(harness.removedDirectories, ["C:\\Temp\\zotero-translate-files-test-job-id"]);
}

async function reportsRequestCountsInProgressAndResult() {
  const harness = makeHarness({
    progressRequests: { requests: 2, planned: 3 },
    requestCount: 3,
    repairRequestCount: 1,
    fragmentItemCount: 2,
    logPath: "C:\\Zotero\\babeldoc-for-zotero\\logs\\job.jsonl",
  });
  const result = await harness.runner.translate(harness.item, {
    onProgress: value => harness.progress.push(value),
  });
  assert.deepEqual(
    JSON.parse(JSON.stringify(harness.progress[0])),
    {
      overall: 42.5,
      stage: "翻译段落",
      current: 3,
      total: 10,
      requests: 2,
      planned: 3,
    },
  );
  assert.equal(result.requestCount, 3);
  assert.equal(result.repairRequestCount, 1);
  assert.equal(result.fragmentItemCount, 2);
  assert.equal(result.logPath, "C:\\Zotero\\babeldoc-for-zotero\\logs\\job.jsonl");
  await result.cleanup();
}

async function exposesLinkRestorationStatsAndMappingPath() {
  const harness = makeHarness({
    links: {
      source: 7,
      restored: 6,
      unresolved: 1,
      unsupported: 0,
      mapping_path: "C:\\Zotero\\babeldoc-for-zotero\\paper.links.json",
    },
  });
  const result = await harness.runner.translate(harness.item, {});
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.linkStats)),
    {
      source: 7,
      restored: 6,
      unresolved: 1,
      unsupported: 0,
      mappingPath: "C:\\Zotero\\babeldoc-for-zotero\\paper.links.json",
    },
  );
  assert.equal(
    result.linkMappingPath,
    "C:\\Zotero\\babeldoc-for-zotero\\paper.links.json",
  );
  await result.cleanup();
}

async function rejectsQueryURLsBeforeStartingBabelDOC() {
  const harness = makeHarness({
    baseURL: "https://api.example.test/v1/chat/completions?tenant=demo",
  });
  await assert.rejects(
    harness.runner.translate(harness.item, {}),
    /BabelDOC 不支持包含查询参数的 API 地址/,
  );
  assert.equal(harness.calls.length, 0, "query URLs must fail before starting BabelDOC");
  assert.equal(harness.createdDirectories.length, 0, "query URLs must fail before creating a job directory");
}

async function errorsAreUsefulAndKeyFree() {
  const harness = makeHarness({
    errorEvent: {
      message: "request failed with RUNNER_SECRET_KEY_789",
      kind: "request_cap",
      failed_paragraphs: 2,
    },
  });
  await assert.rejects(
    harness.runner.translate(harness.item, {}),
    error => {
      assert.ok(error.message.includes("request failed"));
      assert.ok(!error.message.includes("BabelDOC 翻译失败"));
      assert.ok(!error.message.includes(harness.apiKey));
      assert.equal(error.kind, "request_cap", "the structured error kind must survive wrapping");
      return true;
    },
  );
  assert.equal(harness.removedDirectories.length, 1, "failed jobs should remove their work directory");
}

async function parsesAndRedactsPartialFailureWarning() {
  const harness = makeHarness({
    warning: {
      failed_paragraphs: 3,
      message: "Connection error with RUNNER_SECRET_KEY_789",
      tables: [{ table: "表 4", page: 4, reason: "no data rows" }],
    },
  });
  const result = await harness.runner.translate(harness.item, {});
  assert.equal(result.warning.failed_paragraphs, 3);
  assert.ok(result.warning.message.includes("Connection error"));
  assert.ok(!result.warning.message.includes(harness.apiKey));
  assert.equal(result.warning.tables.length, 1);
  assert.equal(result.warning.tables[0].table, "表 4");
  assert.equal(result.warning.tables[0].page, 4);
  assert.equal(result.warning.tables[0].reason, "no data rows");
  await result.cleanup();
}

async function nonZeroExitFailsAndRedactsStderr() {
  const harness = makeHarness({ exitCode: 2 });
  await assert.rejects(
    harness.runner.translate(harness.item, {}),
    error => {
      assert.ok(error.message.includes("退出码 2"));
      assert.ok(error.message.includes("[已隐藏]"));
      assert.ok(!error.message.includes(harness.apiKey));
      assert.ok(!error.message.includes("boundary-bearer-secret"));
      assert.ok(!error.message.includes("sk-1234567890"));
      return true;
    },
  );
}

async function tracebackDiagnosticsShowTheFinalException() {
  const harness = makeHarness({
    exitCode: 1,
    stderrText: [
      "Traceback (most recent call last):",
      "  File \"runner.py\", line 10, in <module>",
      "    raise RuntimeError('provider failed')",
      "RuntimeError: provider failed",
    ].join("\r\n"),
  });
  await assert.rejects(
    harness.runner.translate(harness.item, {}),
    error => {
      assert.ok(error.message.includes("RuntimeError: provider failed"));
      assert.ok(!error.message.includes("Traceback (most recent call last):"));
      return true;
    },
  );
}

async function findsOutputWhenNoResultLineWasEmitted() {
  const harness = makeHarness({
    omitResult: true,
    directoryEntries(directory, callback, files) {
      const fallbackPath = directory + "\\fallback.zh.mono.pdf";
      files.set(fallbackPath, { exists: true, isFile: true, fileSize: 20 });
      callback({ name: "fallback.zh.mono.pdf", path: fallbackPath, isDir: false });
    },
  });
  const result = await harness.runner.translate(harness.item, {});
  assert.equal(
    result.outputPath,
    "C:\\Temp\\zotero-translate-files-test-job-id\\fallback.zh.mono.pdf",
  );
  await result.cleanup();
}

async function missingOrEmptyOutputFailsEvenAfterExitZero() {
  const harness = makeHarness({ omitResult: true, outputSize: 0 });
  await assert.rejects(
    harness.runner.translate(harness.item, {}),
    /未生成有效的中文 PDF/,
  );
}

async function abortKillsTheChildAndCleansUp() {
  const harness = makeHarness({ waitForExit: true });
  const controller = new AbortController();
  const pending = harness.runner.translate(harness.item, { signal: controller.signal });
  await harness.processStarted;
  controller.abort();
  await assert.rejects(pending, /翻译任务已取消/);
  assert.equal(harness.killCount, 1);
  assert.equal(harness.removedDirectories.length, 1);
}

async function supportsBilingualOutputs() {
  for (const mode of ["mono", "dual", "both"]) {
    const harness = makeHarness({ outputMode: mode });
    const result = await harness.runner.translate(harness.item, {});
    const args = harness.calls[0].arguments;
    if (mode === "mono") {
      assert.ok(args.includes("--no-dual"), "mono keeps a single output");
      assert.equal(result.outputPaths.map(entry => entry.kind).join(","), "mono");
    } else {
      assert.ok(!args.includes("--no-dual"), mode + " requests the dual PDF");
      assert.equal(
        result.outputPaths.map(entry => entry.kind).join(","),
        mode === "both" ? "mono,dual" : "dual",
      );
      assert.equal(result.outputPath, result.outputPaths[0].path);
    }
    await result.cleanup();
  }
}

(async () => {
  await copiesResourcesLaunchesSafelyAndParsesProgress();
  await supportsBilingualOutputs();
  await reportsRequestCountsInProgressAndResult();
  await exposesLinkRestorationStatsAndMappingPath();
  await rejectsQueryURLsBeforeStartingBabelDOC();
  await errorsAreUsefulAndKeyFree();
  await parsesAndRedactsPartialFailureWarning();
  await nonZeroExitFailsAndRedactsStderr();
  await tracebackDiagnosticsShowTheFinalException();
  await findsOutputWhenNoResultLineWasEmitted();
  await missingOrEmptyOutputFailsEvenAfterExitZero();
  await abortKillsTheChildAndCleansUp();
  process.stdout.write("translation runner regression cases passed\n");
})().catch(error => {
  process.stderr.write(error.stack + "\n");
  process.exitCode = 1;
});
