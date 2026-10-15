/* global Zotero */

var ZoteroTranslateFilesAttachments = (function () {
  var TRANSLATED_TITLE_SUFFIX = "（中文译本）";
  // Task 6's importer will add this stable marker to every generated attachment.
  var TRANSLATED_ATTACHMENT_TAG = "zotero-translate-files:translated";

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
    return tags.some(function (tag) {
      return (typeof tag === "string" ? tag : tag && tag.tag) === TRANSLATED_ATTACHMENT_TAG;
    });
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
    isTranslatedAttachment: isTranslatedAttachment,
    isTranslatablePDF: isTranslatablePDF,
    resolvePDFAttachments: resolvePDFAttachments,
  };
})();
