/* global Zotero, ZoteroTranslateFilesAttachments */

var ZoteroTranslateFilesImporter = (function () {
  var attachmentRules = ZoteroTranslateFilesAttachments;

  function getOriginalFileName(originalPath) {
    var originalFile = Zotero.File.pathToFile(originalPath);
    var fileName = String(originalFile && originalFile.leafName || "");
    if (!fileName) throw new Error("无法读取原 PDF 文件名。");
    return fileName;
  }

  function getTranslatedFileBaseName(originalFileName) {
    var originalStem = String(originalFileName).replace(/\.pdf$/i, "");
    var translatedFileName = Zotero.File.getValidFileName(originalStem + ".zh.pdf");
    if (!translatedFileName) throw new Error("无法生成译本文件名。");

    // Zotero appends the source extension to fileBaseName, so pass the sanitized stem.
    return translatedFileName.replace(/\.pdf$/i, "");
  }

  async function importResult(original, outputPath) {
    if (!original || !outputPath) throw new Error("缺少原附件或译本 PDF 文件。");

    var originalPath = await original.getFilePathAsync();
    if (!originalPath) throw new Error("无法读取原 PDF 文件名。");

    var originalFileName = getOriginalFileName(originalPath);
    var originalTitle = String(original.getField("title") || "");
    var translatedTitle = originalTitle + attachmentRules.TRANSLATED_TITLE_SUFFIX;
    var parentItemID = original.parentItemID;
    var importOptions = {
      file: outputPath,
      parentItemID: parentItemID ? parentItemID : undefined,
      libraryID: original.libraryID,
      title: translatedTitle,
      contentType: "application/pdf",
      fileBaseName: getTranslatedFileBaseName(originalFileName),
    };

    if (!parentItemID) {
      // Zotero accepts collections for standalone attachments, not together with parentItemID.
      importOptions.collections = original.getCollections() || [];
    }

    // Zotero inserts the attachment before importFromFile returns (it then waits for indexing).
    // Reserve its identity during that gap so a parent-item menu click cannot enqueue it.
    var endPendingImport = attachmentRules.beginTranslatedImport(original, translatedTitle);
    try {
      // Always import a new attachment, even when the same title already exists under this parent.
      var translatedAttachment = await Zotero.Attachments.importFromFile(importOptions);
      if (!translatedAttachment) throw new Error("Zotero 未能创建译本附件。");

      try {
        translatedAttachment.addTag(attachmentRules.TRANSLATED_ATTACHMENT_TAG, 1);
        await translatedAttachment.saveTx();
      } catch (error) {
        // Do not leave an unmarked translation that can be offered to the queue again.
        await translatedAttachment.eraseTx();
        throw error;
      }
      return translatedAttachment;
    } finally {
      endPendingImport();
    }
  }

  return { importResult: importResult };
})();
