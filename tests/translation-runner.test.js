const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const runnerSource = fs.readFileSync(
  path.join(__dirname, "../addon/content/modules/runner.js"),
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

      const progressLine = "ZTF_PROGRESS {\"overall\":42.5,\"stage\":\"翻译段落\",\"current\":3,\"total\":10}\r\n";
      const resultLine = options.errorEvent
        ? "ZTF_ERROR " + JSON.stringify(options.errorEvent === true
          ? { error: "request failed with " + apiKey }
          : options.errorEvent) + "\r\n"
        : options.omitResult
          ? ""
          : "ZTF_RESULT {\"mono\":" + JSON.stringify(outputPath) + "}\r\n";
      const warningLine = options.warning
        ? "ZTF_WARNING " + JSON.stringify(options.warning) + "\r\n"
        : "";
      const stdout = makeStream(progressLine + warningLine + resultLine);
      const stderr = makeStream("diagnostic includes " + apiKey + "\r\n");

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
      File: fileAPI,
      DataDirectory: { dir: "C:\\Zotero" },
    },
    ZoteroTranslateFiles: { rootURI: "jar:file:///plugin.xpi!/" },
    ZoteroTranslateFilesConfig: {
      getSettings: () => ({
        apiKey,
        baseURL: "https://api.example.test/v1/chat/completions",
        model: "test-model",
        babeldocPath: "C:\\babeldoc\\Scripts\\babeldoc.exe",
      }),
      redact(value, configuredKey) {
        let text = String(value == null ? "" : value);
        const secret = configuredKey == null ? apiKey : configuredKey;
        if (secret) text = text.split(String(secret)).join("[已隐藏]");
        return text
          .replace(/\bBearer\s+\S+/gi, "Bearer [已隐藏]")
          .replace(/\bsk-[A-Za-z0-9_-]{8,}\b/g, "[已隐藏 API key]");
      },
      resolvePythonFromBabelDOCPath: () => pythonPath,
      openAIBaseURLForBabelDOC: value => value.replace(/\/chat\/completions$/, ""),
    },
  };
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
  assert.ok(harness.createdDirectories.includes("C:\\Zotero\\zotero-translate-files"));
  assert.equal(harness.createdDirectories.length, 3, "data, job, and BabelDOC working directories should be created");

  const call = harness.calls[0];
  const args = call.arguments;
  assert.equal(call.command, "C:\\babeldoc\\python.exe");
  assert.ok(args.includes("--no-dual"));
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

async function errorsAreUsefulAndKeyFree() {
  const harness = makeHarness({
    errorEvent: {
      message: "request failed with RUNNER_SECRET_KEY_789",
      kind: "translation_failed",
      failed_paragraphs: 2,
    },
  });
  await assert.rejects(
    harness.runner.translate(harness.item, {}),
    error => {
      assert.ok(error.message.includes("request failed"));
      assert.ok(!error.message.includes("BabelDOC 翻译失败"));
      assert.ok(!error.message.includes(harness.apiKey));
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
    },
  });
  const result = await harness.runner.translate(harness.item, {});
  assert.equal(result.warning.failed_paragraphs, 3);
  assert.ok(result.warning.message.includes("Connection error"));
  assert.ok(!result.warning.message.includes(harness.apiKey));
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

(async () => {
  await copiesResourcesLaunchesSafelyAndParsesProgress();
  await errorsAreUsefulAndKeyFree();
  await parsesAndRedactsPartialFailureWarning();
  await nonZeroExitFailsAndRedactsStderr();
  await findsOutputWhenNoResultLineWasEmitted();
  await missingOrEmptyOutputFailsEvenAfterExitZero();
  await abortKillsTheChildAndCleansUp();
  process.stdout.write("translation runner regression cases passed\n");
})().catch(error => {
  process.stderr.write(error.stack + "\n");
  process.exitCode = 1;
});
