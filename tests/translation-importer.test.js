const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const items = new Map();

function makeImportedAttachment(options, id) {
  const tags = [];
  return {
    id,
    parentItemID: options.parentItemID,
    libraryID: options.libraryID,
    getField(field) {
      return field === "title" ? options.title : "";
    },
    getTags() {
      return tags;
    },
    getCollections() {
      return options.collections || [];
    },
    isPDFAttachment() {
      return true;
    },
    isStoredFileAttachment() {
      return true;
    },
    isLinkedFileAttachment() {
      return false;
    },
    addTag(tag, type) {
      tags.push({ tag, type });
    },
    async saveTx() {
      this.saved = true;
    },
  };
}

const context = {
  Zotero: {
    Items: { get: id => items.get(id) },
    File: {
      getValidFileNameCalls: [],
      pathToFile(filePath) {
        const leafName = String(filePath).split(/[\\/]/).pop();
        return { leafName };
      },
      getValidFileName(fileName) {
        this.getValidFileNameCalls.push(fileName);
        return fileName.replace(/[<>:"/\\|?*]/g, "_");
      },
    },
    Attachments: {
      calls: [],
      async importFromFile(options) {
        this.calls.push(options);
        const item = makeImportedAttachment(options, 1000 + this.calls.length);
        items.set(item.id, item);
        return item;
      },
    },
  },
};

for (const file of [
  "../addon/content/modules/attachments.js",
  "../addon/content/modules/importer.js",
]) {
  const source = fs.readFileSync(path.join(__dirname, file), "utf8");
  vm.runInNewContext(source, context, { filename: file });
}

function makeOriginal(options = {}) {
  return {
    id: options.id || 500,
    parentItemID: options.parentItemID,
    libraryID: 12,
    getField(field) {
      return field === "title" ? "Original title" : "";
    },
    async getFilePathAsync() {
      return "C:\\Papers\\Smith: 2020? - Title.pdf";
    },
    getCollections() {
      return options.collections || [4, 9];
    },
    getTags() {
      return [];
    },
    isPDFAttachment() {
      return true;
    },
    isStoredFileAttachment() {
      return true;
    },
    isLinkedFileAttachment() {
      return false;
    },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise(resolvePromise => { resolve = resolvePromise; });
  return { promise, resolve };
}

(async () => {
  const importer = context.ZoteroTranslateFilesImporter;
  const childOriginal = makeOriginal({ parentItemID: 45 });
  const translated = await importer.importResult(childOriginal, "C:\\Temp\\output.pdf");
  const childOptions = context.Zotero.Attachments.calls[0];

  assert.equal(childOptions.file, "C:\\Temp\\output.pdf");
  assert.equal(childOptions.parentItemID, 45);
  assert.equal(childOptions.libraryID, 12);
  assert.equal(childOptions.title, "Original title（中文译本）");
  assert.equal(childOptions.contentType, "application/pdf");
  assert.equal(childOptions.fileBaseName, "Smith_ 2020_ - Title.zh");
  assert.equal("collections" in childOptions, false,
    "child attachments use parentItemID rather than collection membership options");
  assert.deepEqual(context.Zotero.File.getValidFileNameCalls, ["Smith: 2020? - Title.zh.pdf"]);
  assert.deepEqual(translated.getTags(), [
    { tag: "zotero-translate-files:translated", type: 1 },
  ]);
  assert.equal(translated.saved, true);
  assert.equal(context.ZoteroTranslateFilesAttachments.isTranslatedAttachment(translated), true);

  const standaloneOriginal = makeOriginal({ parentItemID: false, collections: [3, 8] });
  const firstStandalone = await importer.importResult(standaloneOriginal, "C:\\Temp\\output.pdf");
  const secondStandalone = await importer.importResult(standaloneOriginal, "C:\\Temp\\output.pdf");
  const standaloneOptions = context.Zotero.Attachments.calls.slice(1);

  assert.equal(standaloneOptions.length, 2,
    "repeated translations create new attachments even when their titles match");
  assert.equal(standaloneOptions[0].parentItemID, undefined);
  assert.deepEqual(standaloneOptions[0].collections, [3, 8]);
  assert.notEqual(firstStandalone.id, secondStandalone.id);
  assert.equal(context.ZoteroTranslateFilesAttachments.isTranslatedAttachment(secondStandalone), true);

  const failingOriginal = makeOriginal({ parentItemID: 80, id: 801 });
  items.set(failingOriginal.id, failingOriginal);
  let failedAttachment;
  const failureAttachmentIDs = [failingOriginal.id];
  const failureParent = {
    isRegularItem: () => true,
    getAttachments: () => failureAttachmentIDs,
  };
  context.Zotero.Attachments.importFromFile = async function (options) {
    this.calls.push(options);
    failedAttachment = makeImportedAttachment(options, 1801);
    items.set(failedAttachment.id, failedAttachment);
    failureAttachmentIDs.push(failedAttachment.id);
    failedAttachment.saveTx = async function () {
      // Zotero rolls the unsaved tag back when this transaction fails.
      this.getTags = () => [];
      throw new Error("tag save failed");
    };
    failedAttachment.eraseTx = async function () {
      this.erased = true;
      items.delete(this.id);
      failureAttachmentIDs.splice(failureAttachmentIDs.indexOf(this.id), 1);
    };
    return failedAttachment;
  };

  await assert.rejects(
    importer.importResult(failingOriginal, "C:\\Temp\\failed.pdf"),
    /tag save failed/,
  );
  assert.equal(failedAttachment.erased, true,
    "a newly imported attachment is erased when saving its marker fails");
  assert.equal(items.has(failedAttachment.id), false,
    "the failed translated attachment must not remain in Zotero");
  assert.equal(context.ZoteroTranslateFilesAttachments.isTranslatedAttachment(failedAttachment), false,
    "the temporary marker is released after cleanup");
  assert.deepEqual(
    Array.from(context.ZoteroTranslateFilesAttachments.resolvePDFAttachments([failureParent]), item => item.id),
    [failingOriginal.id],
    "a failed unmarked translation must not remain available for enqueueing",
  );

  const pendingOriginal = makeOriginal({ parentItemID: 70, id: 701 });
  items.set(pendingOriginal.id, pendingOriginal);
  const attachmentIDs = [pendingOriginal.id];
  const parent = {
    isRegularItem: () => true,
    getAttachments: () => attachmentIDs,
  };
  const importStarted = deferred();
  const allowImportToReturn = deferred();
  context.Zotero.Attachments.importFromFile = function (options) {
    this.calls.push(options);
    const item = makeImportedAttachment(options, 2001);
    items.set(item.id, item);
    attachmentIDs.push(item.id);
    importStarted.resolve(item);
    return allowImportToReturn.promise.then(() => item);
  };

  const delayedImport = importer.importResult(pendingOriginal, "C:\\Temp\\delayed.pdf");
  const insertedButUnmarked = await importStarted.promise;
  assert.deepEqual(insertedButUnmarked.getTags(), [], "Zotero has inserted it but importer has not tagged it yet");
  assert.equal(
    context.ZoteroTranslateFilesAttachments.isTranslatedAttachment(insertedButUnmarked),
    true,
    "the in-flight import reservation must classify the new attachment as translated",
  );
  assert.deepEqual(
    Array.from(context.ZoteroTranslateFilesAttachments.resolvePDFAttachments([parent]), item => item.id),
    [pendingOriginal.id],
    "repeated translation from the parent must omit the inserted, not-yet-tagged attachment",
  );

  allowImportToReturn.resolve();
  const taggedTranslation = await delayedImport;
  assert.equal(context.ZoteroTranslateFilesAttachments.isTranslatedAttachment(taggedTranslation), true);
  assert.deepEqual(
    Array.from(context.ZoteroTranslateFilesAttachments.resolvePDFAttachments([parent]), item => item.id),
    [pendingOriginal.id],
    "the persistent tag continues to filter the attachment after import completes",
  );

  process.stdout.write("translation import regression cases passed\n");
})().catch(error => {
  process.stderr.write(error.stack + "\n");
  process.exitCode = 1;
});
