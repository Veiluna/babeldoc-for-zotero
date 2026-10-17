const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

for (const locale of ["zh-CN", "en-US"]) {
  const resource = fs.readFileSync(
    path.join(__dirname, `../addon/locale/${locale}/zotero-translate-files.ftl`),
    "utf8",
  );
  for (const messageID of [
    "zotero-translate-files-import-tag-warning-title",
    "zotero-translate-files-import-tag-warning-description",
    "zotero-translate-files-import-tag-warning-default-name",
  ]) {
    assert.match(resource, new RegExp(`^${messageID}\\s*=`, "m"),
      `${locale} defines ${messageID}`);
  }
}

const items = new Map();
const parentAttachmentIDs = new Map();
const warningWindows = [];
const timerDelays = [];
const tagRows = new Map();
const tagCache = new Map();
const localizedMessageCalls = [];
let nextTagID = 129;
let tagCacheInitCalls = 0;
let itemGetCalls = 0;
let existsCalls = 0;
let getAllCalls = 0;

function makeParent(id) {
  if (!items.has(id)) {
    const attachmentIDs = [];
    parentAttachmentIDs.set(id, attachmentIDs);
    items.set(id, {
      id,
      libraryID: 12,
      getAttachments() {
        return attachmentIDs.slice();
      },
    });
  }
  return items.get(id);
}

function addToParent(item) {
  if (!item.parentItemID) return;
  const parent = makeParent(item.parentItemID);
  const attachmentIDs = parentAttachmentIDs.get(parent.id);
  if (!attachmentIDs.includes(item.id)) attachmentIDs.push(item.id);
}

function putItem(item) {
  items.set(item.id, item);
  addToParent(item);
  return item;
}

function removeItem(item) {
  items.delete(item.id);
  if (item.parentItemID && parentAttachmentIDs.has(item.parentItemID)) {
    const attachmentIDs = parentAttachmentIDs.get(item.parentItemID);
    const index = attachmentIDs.indexOf(item.id);
    if (index >= 0) attachmentIDs.splice(index, 1);
  }
}

function makeImportedAttachment(options, id, dateAdded = "2022-01-01 00:00:00") {
  const tags = [];
  const item = {
    id,
    parentItemID: options.parentItemID,
    libraryID: options.libraryID,
    dateAdded,
    eraseCalls: 0,
    saved: false,
    getField(field) {
      return field === "title" ? options.title : "";
    },
    getTags() {
      return tags;
    },
    getCollections() {
      return options.collections || [];
    },
    isAttachment() {
      return true;
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
      const tagID = context.Zotero.Tags.getID(tag);
      if (!tagID || tagRows.get(tag) !== tagID) {
        throw new Error("FOREIGN KEY constraint failed in tags");
      }
      tags.push({ tag, type });
    },
    async saveTx() {
      for (const tag of tags) {
        const tagID = context.Zotero.Tags.getID(tag.tag);
        if (!tagID || tagRows.get(tag.tag) !== tagID) {
          throw new Error("FOREIGN KEY constraint failed in itemTags");
        }
      }
      this.saved = true;
    },
    async eraseTx() {
      this.eraseCalls++;
      removeItem(this);
    },
  };
  return item;
}

const context = {
  Zotero: {
    Items: {
      get(id) {
        itemGetCalls++;
        return items.get(Number(id)) || false;
      },
      exists(id) {
        existsCalls++;
        return items.has(Number(id));
      },
      async getAll(libraryID) {
        getAllCalls++;
        return Array.from(items.values()).filter(item => item.libraryID === libraryID);
      },
    },
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
    ProgressWindow: function ProgressWindow() {
      const warning = {
        headline: "",
        itemProgress: null,
        addDescriptionCalls: 0,
        shown: false,
        closeTimer: 0,
      };
      warningWindows.push(warning);
      const progress = {
        changeHeadline(headline) {
          warning.headline = headline;
        },
        ItemProgress: function ItemProgress(icon, label) {
          warning.itemProgress = { icon, label, text: "", textNode: null };
          return {
            setText(text) {
              // Model Zotero's createTextNode path: the value is never parsed as markup.
              warning.itemProgress.textNode = { nodeType: 3, nodeValue: String(text) };
              warning.itemProgress.text = String(text);
            },
          };
        },
        show() {
          warning.shown = true;
        },
        startCloseTimer(milliseconds) {
          warning.closeTimer = milliseconds;
        },
      };
      progress.addDescription = function () {
        warning.addDescriptionCalls++;
      };
      return progress;
    },
    Attachments: {
      calls: [],
      async importFromFile(options) {
        this.calls.push(options);
        return putItem(makeImportedAttachment(options, 1000 + this.calls.length));
      },
    },
    DB: {
      async valueQueryAsync(_query, params) {
        return tagRows.has(params[0]) ? tagRows.get(params[0]) : null;
      },
      async executeTransaction(callback) {
        const rowsBefore = new Map(tagRows);
        try {
          return await callback();
        } catch (error) {
          tagRows.clear();
          for (const [name, id] of rowsBefore) tagRows.set(name, id);
          // Match Zotero 10.0.6: the in-memory Tags cache is not rolled back.
          throw error;
        }
      },
    },
    Tags: {
      getID(name) {
        return tagCache.has(name) ? tagCache.get(name) : false;
      },
      async init() {
        tagCacheInitCalls++;
        tagCache.clear();
        for (const [name, id] of tagRows) tagCache.set(name, id);
      },
      create(name) {
        if (tagCache.has(name)) return tagCache.get(name);
        const id = nextTagID++;
        tagRows.set(name, id);
        tagCache.set(name, id);
        return id;
      },
    },
    getMainWindow() {
      return {
        MozXULElement: {
          insertFTLIfNeeded(resource) {
            assert.equal(resource, "zotero-translate-files.ftl");
          },
        },
        document: {
          l10n: {
            async formatValue(messageID, args) {
              localizedMessageCalls.push({ messageID, args });
              if (messageID === "zotero-translate-files-import-tag-warning-title") {
                return "译本已添加，但无法标记为已翻译";
              }
              if (messageID === "zotero-translate-files-import-tag-warning-default-name") {
                return "中文译本";
              }
              if (messageID === "zotero-translate-files-import-tag-warning-description") {
                return `${args.title} 已添加到 Zotero，但未能添加“已翻译”标记。`;
              }
              return messageID;
            },
          },
        },
      };
    },
  },
  setTimeout(callback, milliseconds) {
    timerDelays.push(milliseconds);
    callback();
    return timerDelays.length;
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
  const original = {
    id: options.id || 500,
    parentItemID: options.parentItemID,
    libraryID: 12,
    dateAdded: "2020-01-01 00:00:00",
    getField(field) {
      return field === "title" ? options.title || "Original title" : "";
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
    isAttachment() {
      return true;
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
  items.set(original.id, original);
  if (original.parentItemID) {
    makeParent(original.parentItemID);
    addToParent(original);
  }
  return original;
}

function makeLinkedReplacement(options, id, dateAdded) {
  const item = makeImportedAttachment(options, id, dateAdded);
  item.isStoredFileAttachment = () => false;
  item.isLinkedFileAttachment = () => true;
  return putItem(item);
}

(async () => {
  const importer = context.ZoteroTranslateFilesImporter;
  const tagName = context.ZoteroTranslateFilesAttachments.TRANSLATED_ATTACHMENT_TAG;
  let rolledBackTagID;
  await assert.rejects(
    context.Zotero.DB.executeTransaction(async () => {
      rolledBackTagID = context.Zotero.Tags.create(tagName);
      throw new Error("simulate tag row rollback with retained cache");
    }),
    /simulate tag row rollback/,
  );
  assert.equal(tagRows.has(tagName), false, "the setup rolls the tag row back");
  assert.equal(context.Zotero.Tags.getID(tagName), rolledBackTagID,
    "the setup retains the stale tag ID in Zotero.Tags cache");

  const childOriginal = makeOriginal({ parentItemID: 45 });
  const existsCallsBeforeNormalImport = existsCalls;
  const getsBeforeNormalImport = itemGetCalls;
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
  assert.equal(existsCalls - existsCallsBeforeNormalImport, 1,
    "the normal path only performs the cheap imported-item existence check");
  assert.equal(getAllCalls, 0, "the normal child-attachment path does not scan the library");
  assert.equal(itemGetCalls - getsBeforeNormalImport, 1,
    "the normal path only reads the parent list to snapshot existing candidate IDs");
  assert.ok(tagCacheInitCalls >= 1, "stale tag cache is reloaded from the database before tagging");
  assert.equal(tagRows.has(tagName), true, "the tag row is committed before item tagging");
  assert.equal(context.Zotero.Tags.getID(tagName), tagRows.get(tagName),
    "the in-memory tag ID agrees with the committed database row");

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

  const replacedBeforeOriginal = makeOriginal({ parentItemID: 80, id: 801 });
  const replaceBeforeParentIDs = parentAttachmentIDs.get(80);
  let erasedOldBefore = 0;
  let replacementBefore;
  let pendingCoveredReplacement = false;
  context.Zotero.Attachments.importFromFile = async function (options) {
    this.calls.push(options);
    const oldItem = makeImportedAttachment(options, 194);
    oldItem.eraseTx = async function () { erasedOldBefore++; };
    putItem(oldItem);
    removeItem(oldItem);
    makeLinkedReplacement(options, 195, "2022-01-02 00:00:00");
    replacementBefore = makeLinkedReplacement(options, 200, oldItem.dateAdded);
    pendingCoveredReplacement = context.ZoteroTranslateFilesAttachments.isTranslatedAttachment(replacementBefore);
    return oldItem;
  };

  const translatedReplacement = await importer.importResult(replacedBeforeOriginal, "C:\\Temp\\replaced.pdf");
  assert.equal(translatedReplacement, replacementBefore,
    "replacement made before tagging is resolved and returned");
  assert.equal(translatedReplacement.id, 200);
  assert.equal(translatedReplacement.saved, true);
  assert.equal(context.ZoteroTranslateFilesAttachments.isTranslatedAttachment(translatedReplacement), true);
  assert.equal(pendingCoveredReplacement, true,
    "the pending-import key covers an untagged replacement with the same parent and title");
  assert.equal(erasedOldBefore, 0);
  assert.deepEqual(items.get(195).getTags(), [], "a replacement with a different dateAdded is not preferred");
  assert.equal(replacementBefore.eraseCalls, 0);
  assert.equal(warningWindows.length, 0);
  assert.ok(replaceBeforeParentIDs.includes(200));

  const standaloneReplacementOriginal = makeOriginal({ parentItemID: false, collections: [3, 8], id: 802 });
  let standaloneReplacement;
  let pendingCoveredStandaloneReplacement = false;
  context.Zotero.Attachments.importFromFile = async function (options) {
    this.calls.push(options);
    const oldItem = makeImportedAttachment(options, 201);
    putItem(oldItem);
    removeItem(oldItem);
    standaloneReplacement = makeLinkedReplacement(options, 202, oldItem.dateAdded);
    pendingCoveredStandaloneReplacement = context.ZoteroTranslateFilesAttachments.isTranslatedAttachment(standaloneReplacement);
    return oldItem;
  };
  const translatedStandaloneReplacement = await importer.importResult(
    standaloneReplacementOriginal,
    "C:\\Temp\\standalone-replaced.pdf",
  );
  assert.equal(translatedStandaloneReplacement, standaloneReplacement,
    "a standalone replacement is resolved by its library and collection membership");
  assert.equal(translatedStandaloneReplacement.saved, true);
  assert.equal(pendingCoveredStandaloneReplacement, true,
    "the pending-import key covers a standalone replacement in the same collections");

  const replacedDuringOriginal = makeOriginal({ parentItemID: 81, id: 811 });
  let oldDuring;
  let replacementDuring;
  let erasedDuring = 0;
  context.Zotero.Attachments.importFromFile = async function (options) {
    this.calls.push(options);
    oldDuring = makeImportedAttachment(options, 196);
    putItem(oldDuring);
    oldDuring.saveTx = async function () {
      removeItem(oldDuring);
      oldDuring.getTags = () => [];
      oldDuring.eraseTx = async function () { erasedDuring++; };
      replacementDuring = makeLinkedReplacement(options, 197, oldDuring.dateAdded);
      throw new Error("FOREIGN KEY constraint failed in itemTags");
    };
    return oldDuring;
  };

  const translatedDuringReplacement = await importer.importResult(replacedDuringOriginal, "C:\\Temp\\during.pdf");
  assert.equal(translatedDuringReplacement, replacementDuring,
    "a failed tag transaction on an erased item resolves the new live attachment");
  assert.equal(translatedDuringReplacement.id, 197);
  assert.equal(translatedDuringReplacement.saved, true);
  assert.equal(context.ZoteroTranslateFilesAttachments.isTranslatedAttachment(translatedDuringReplacement), true);
  assert.equal(erasedDuring, 0, "the importer never erases an item replaced by another plugin");
  assert.equal(replacementDuring.eraseCalls, 0);

  const untaggableOriginal = makeOriginal({ parentItemID: 82, id: 821 });
  let untaggable;
  context.Zotero.Attachments.importFromFile = async function (options) {
    this.calls.push(options);
    untaggable = makeImportedAttachment(options, 198);
    untaggable.saveTx = async function () {
      this.getTags = () => [];
      throw new Error("tag save unavailable");
    };
    return putItem(untaggable);
  };

  const warningCountBefore = warningWindows.length;
  const tagWarningResult = await importer.importResult(untaggableOriginal, "C:\\Temp\\untaggable.pdf");
  assert.equal(tagWarningResult, untaggable,
    "a live translation is returned even when its marker cannot be saved");
  assert.equal(tagWarningResult.zoteroTranslateFilesTagWarning, true);
  assert.equal(items.has(untaggable.id), true, "a tag failure never erases the translated PDF");
  assert.equal(untaggable.eraseCalls, 0);
  assert.equal(warningWindows.length, warningCountBefore + 1);
  const tagWarningWindow = warningWindows[warningWindows.length - 1];
  assert.equal(tagWarningWindow.headline, "译本已添加，但无法标记为已翻译");
  assert.match(tagWarningWindow.itemProgress.text, /已添加到 Zotero，但未能添加“已翻译”标记/);
  assert.equal(tagWarningWindow.shown, true);
  assert.equal(tagWarningWindow.closeTimer, 5000);
  assert.equal(tagWarningWindow.addDescriptionCalls, 0,
    "warning text uses ItemProgress.setText instead of the markup-parsing description API");
  assert.equal(tagWarningWindow.itemProgress.textNode.nodeType, 3,
    "warning body is rendered as a text node");
  assert.equal(tagWarningWindow.itemProgress.label, "中文译本",
    "the default attachment name comes from Fluent");
  assert.ok(localizedMessageCalls.some(call =>
    call.messageID === "zotero-translate-files-import-tag-warning-description"));

  const injectionTitle = '<a onmouseover="void(0)">paper</a>';
  const injectionOriginal = makeOriginal({ parentItemID: 85, id: 851, title: injectionTitle });
  let injectionAttachment;
  context.Zotero.Attachments.importFromFile = async function (options) {
    this.calls.push(options);
    injectionAttachment = makeImportedAttachment(options, 205);
    injectionAttachment.saveTx = async function () {
      throw new Error("tag save unavailable");
    };
    return putItem(injectionAttachment);
  };
  const injectionWarningCountBefore = warningWindows.length;
  await importer.importResult(injectionOriginal, "C:\\Temp\\injection-title.pdf");
  const injectionWarning = warningWindows[injectionWarningCountBefore];
  assert.ok(injectionWarning.itemProgress.textNode.nodeValue.includes(injectionTitle),
    "an attachment title with an event attribute is displayed unchanged as text");
  assert.equal(injectionWarning.itemProgress.textNode.nodeType, 3);
  assert.equal(injectionWarning.addDescriptionCalls, 0);

  const missingReplacementOriginal = makeOriginal({ parentItemID: 83, id: 831 });
  let missingReplacementItem;
  let missingReplacementEraseCalls = 0;
  const delaysBeforeMissingReplacement = timerDelays.length;
  context.Zotero.Attachments.importFromFile = async function (options) {
    this.calls.push(options);
    missingReplacementItem = makeImportedAttachment(options, 199);
    missingReplacementItem.eraseTx = async function () { missingReplacementEraseCalls++; };
    missingReplacementItem.saveTx = async function () {
      removeItem(missingReplacementItem);
      throw new Error("FOREIGN KEY constraint failed in itemTags");
    };
    return putItem(missingReplacementItem);
  };

  const missingReplacementResult = await importer.importResult(missingReplacementOriginal, "C:\\Temp\\missing.pdf");
  assert.equal(missingReplacementResult, missingReplacementItem,
    "the importer returns a usable title-bearing item reference if replacement lookup is exhausted");
  assert.equal(missingReplacementResult.zoteroTranslateFilesTagWarning, true);
  assert.equal(missingReplacementEraseCalls, 0);
  assert.equal(timerDelays.length - delaysBeforeMissingReplacement, 2,
    "replacement lookup is bounded to three scans with two 500 ms waits");
  assert.deepEqual(timerDelays.slice(-2), [500, 500]);

  const failedImportOriginal = makeOriginal({ parentItemID: 84, id: 841 });
  const unrelatedImportError = new Error("importFromFile failed");
  context.Zotero.Attachments.importFromFile = async function (options) {
    this.calls.push(options);
    throw unrelatedImportError;
  };
  await assert.rejects(
    importer.importResult(failedImportOriginal, "C:\\Temp\\failed-import.pdf"),
    error => error === unrelatedImportError,
    "failures from importFromFile itself still propagate",
  );

  process.stdout.write("translation import regression cases passed\n");
})().catch(error => {
  process.stderr.write(error.stack + "\n");
  process.exitCode = 1;
});
