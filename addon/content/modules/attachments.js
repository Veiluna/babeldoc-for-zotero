/* global Zotero */

var ZoteroTranslateFilesAttachments = (function () {
  var TRANSLATED_TITLE_SUFFIX = "（中文译本）";
  // Task 6's importer will add this stable marker to every generated attachment.
  var TRANSLATED_ATTACHMENT_TAG = "zotero-translate-files:translated";
  var pendingTranslatedImports = new Map();

  function pendingImportKey(item, title) {
    var parentItemID = item.parentItemID || null;
    var collections = [];
    if (!parentItemID && typeof item.getCollections === "function") {
      collections = (item.getCollections() || []).map(String).sort();
    }
    return JSON.stringify([
      String(item.libraryID == null ? "" : item.libraryID),
      String(parentItemID == null ? "" : parentItemID),
      String(title),
      collections,
    ]);
  }

  function beginTranslatedImport(original, translatedTitle) {
    var key = pendingImportKey(original, translatedTitle);
    pendingTranslatedImports.set(key, (pendingTranslatedImports.get(key) || 0) + 1);
    var active = true;

    return function endTranslatedImport() {
      if (!active) return;
      active = false;
      var count = pendingTranslatedImports.get(key) || 0;
      if (count <= 1) pendingTranslatedImports.delete(key);
      else pendingTranslatedImports.set(key, count - 1);
    };
  }

  function isPendingTranslatedImport(item, title) {
    try {
      return pendingTranslatedImports.has(pendingImportKey(item, title));
    } catch (error) {
      return false;
    }
  }

  function isTranslatedAttachment(item) {
    if (!item || typeof item.getField !== "function" || typeof item.getTags !== "function") {
      return false;
    }

    var title;
    var tags;
    try {
      title = String(item.getField("title") || "");
      tags = item.getTags() || [];
    } catch (error) {
      return false;
    }

    if (!title.endsWith(TRANSLATED_TITLE_SUFFIX)) return false;
    if (tags.some(function (tag) {
      return (typeof tag === "string" ? tag : tag && tag.tag) === TRANSLATED_ATTACHMENT_TAG;
    })) return true;
    return isPendingTranslatedImport(item, title);
  }

  function isTranslatablePDF(item) {
    if (!item || typeof item.isPDFAttachment !== "function" || !item.isPDFAttachment()) {
      return false;
    }

    var isStoredFile =
      typeof item.isStoredFileAttachment === "function" && item.isStoredFileAttachment();
    var isLinkedFile =
      typeof item.isLinkedFileAttachment === "function" && item.isLinkedFileAttachment();
    return (isStoredFile || isLinkedFile) && !isTranslatedAttachment(item);
  }

  function resolvePDFAttachments(selectedItems) {
    var attachments = [];
    var seenIDs = new Set();

    function addIfTranslatable(item) {
      if (!isTranslatablePDF(item) || item.id == null) return;
      var id = String(item.id);
      if (seenIDs.has(id)) return;
      seenIDs.add(id);
      attachments.push(item);
    }

    for (var selectedItem of selectedItems || []) {
      if (!selectedItem) continue;

      if (isTranslatablePDF(selectedItem)) {
        addIfTranslatable(selectedItem);
        continue;
      }

      if (typeof selectedItem.isRegularItem !== "function" || !selectedItem.isRegularItem()) {
        continue;
      }

      var attachmentIDs;
      try {
        attachmentIDs = selectedItem.getAttachments() || [];
      } catch (error) {
        continue;
      }

      for (var attachmentID of attachmentIDs) {
        var attachment;
        try {
          attachment = Zotero.Items.get(attachmentID);
        } catch (error) {
          continue;
        }
        addIfTranslatable(attachment);
      }
    }

    return attachments;
  }

  return {
    TRANSLATED_TITLE_SUFFIX: TRANSLATED_TITLE_SUFFIX,
    TRANSLATED_ATTACHMENT_TAG: TRANSLATED_ATTACHMENT_TAG,
    beginTranslatedImport: beginTranslatedImport,
    isTranslatedAttachment: isTranslatedAttachment,
    isTranslatablePDF: isTranslatablePDF,
    resolvePDFAttachments: resolvePDFAttachments,
  };
})();
