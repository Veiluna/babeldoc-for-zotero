const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../addon/content/modules/attachments.js"),
  "utf8"
);

function makeAttachment(id, options = {}) {
  return {
    id,
    getField(field) {
      return field === "title" ? options.title || "paper.pdf" : "";
    },
    getTags() {
      return options.tags || [];
    },
    isPDFAttachment() {
      return options.pdf !== false;
    },
    isStoredFileAttachment() {
      return options.stored === true;
    },
    isLinkedFileAttachment() {
      return options.linked === true;
    },
    isRegularItem() {
      return false;
    },
  };
}

function makeRegularItem(id, attachmentIDs) {
  return {
    id,
    isRegularItem() {
      return true;
    },
    getAttachments() {
      return attachmentIDs;
    },
  };
}

const translatedID = "babeldoc-for-zotero:translated";
const legacyTranslatedID = "zotero-translate-files:translated";
const attachmentsByID = new Map([
  [1, makeAttachment(1, { stored: true })],
  [2, makeAttachment(2, { linked: true })],
  [3, makeAttachment(3, { stored: true, pdf: false })],
  [4, makeAttachment(4, { title: "paper（中文译本）", stored: true })],
  [5, makeAttachment(5, {
    title: "paper（中文译本）",
    stored: true,
    tags: [{ tag: translatedID }],
  })],
  [6, makeAttachment(6, { stored: true })],
  [7, makeAttachment(7, {
    title: "paper（中文译本）",
    stored: true,
    tags: [{ tag: legacyTranslatedID }],
  })],
]);

const context = {
  Zotero: {
    Items: {
      get(id) {
        return attachmentsByID.get(id);
      },
    },
  },
};
vm.runInNewContext(source, context, { filename: "attachments.js" });

const attachments = context.ZoteroTranslateFilesAttachments;
assert.equal(attachments.TRANSLATED_ATTACHMENT_TAG, translatedID);
assert.equal(attachments.isTranslatedAttachment(attachmentsByID.get(5)), true);
assert.equal(attachments.isTranslatedAttachment(attachmentsByID.get(7)), true,
  "legacy translated tags still identify old plugin output");
assert.equal(attachments.isTranslatedAttachment(attachmentsByID.get(4)), false,
  "the translated title suffix alone must not classify a user attachment as plugin output");

const selectedItems = [
  makeRegularItem(100, [1, 2, 3, 4, 5, 6, 7]),
  attachmentsByID.get(1),
  { isNote: () => true },
];
const resolvedIDs = Array.from(attachments.resolvePDFAttachments(selectedItems), item => item.id);
assert.deepEqual(resolvedIDs, [1, 2, 4, 6],
  "regular items resolve stored and linked PDFs, while filtering non-PDFs, marked outputs, and duplicates");
assert.equal(attachments.isTranslatablePDF(attachmentsByID.get(3)), false);
assert.equal(attachments.isTranslatablePDF(makeAttachment(7)), false,
  "PDF attachments without a stored or linked file are not translatable");

process.stdout.write("translation attachment selection regression cases passed\n");
