/* global Zotero, ZoteroTranslateFilesAttachments */

var ZoteroTranslateFilesImporter = (function () {
  var attachmentRules = ZoteroTranslateFilesAttachments;
  var MAX_REPLACEMENT_LOOKUPS = 3;
  var REPLACEMENT_RETRY_DELAY_MS = 500;
  var TAG_WARNING_PROPERTY = "zoteroTranslateFilesTagWarning";

  function getOriginalFileName(originalPath) {
    var originalFile = Zotero.File.pathToFile(originalPath);
    var fileName = String(originalFile && originalFile.leafName || "");
    if (!fileName) throw new Error("无法读取原 PDF 文件名。");
    return fileName;
  }

  function getTranslatedFileBaseName(originalFileName, kind) {
    var originalStem = String(originalFileName).replace(/\.pdf$/i, "");
    var suffix = kind === "dual" ? ".zh.dual.pdf" : ".zh.pdf";
    var translatedFileName = Zotero.File.getValidFileName(originalStem + suffix);
    if (!translatedFileName) throw new Error("无法生成译本文件名。");

    // Zotero appends the source extension to fileBaseName, so pass the sanitized stem.
    return translatedFileName.replace(/\.pdf$/i, "");
  }

  function getItem(id) {
    try {
      return Zotero.Items.get(id) || null;
    } catch (error) {
      return null;
    }
  }

  function itemExists(item) {
    if (!item || item.id == null) return false;
    try {
      return Zotero.Items.exists(item.id) && !item.deleted;
    } catch (error) {
      return false;
    }
  }

  function addIDs(target, items) {
    for (var item of items || []) {
      var id = item && typeof item === "object" ? item.id : item;
      if (id != null) target.add(String(id));
    }
  }

  function getCollections(item) {
    try {
      return (item.getCollections() || []).map(String).sort();
    } catch (error) {
      return [];
    }
  }

  function sameCollections(first, second) {
    var firstIDs = getCollections(first);
    var secondIDs = getCollections(second);
    return firstIDs.length === secondIDs.length && firstIDs.every(function (id, index) {
      return id === secondIDs[index];
    });
  }

  function sameImportScope(candidate, original) {
    if (!candidate || String(candidate.libraryID) !== String(original.libraryID)) return false;

    if (original.parentItemID) {
      return String(candidate.parentItemID || "") === String(original.parentItemID);
    }
    return !candidate.parentItemID && sameCollections(candidate, original);
  }

  function isMatchingAttachment(candidate, original, title) {
    if (!candidate || candidate.deleted || candidate.id == null) return false;
    if (typeof candidate.isAttachment !== "function" || !candidate.isAttachment()) return false;
    if (!sameImportScope(candidate, original)) return false;

    try {
      return String(candidate.getField("title") || "") === title;
    } catch (error) {
      return false;
    }
  }

  async function getScopedItems(original) {
    if (original.parentItemID) {
      var parent = getItem(original.parentItemID);
      if (parent && typeof parent.getAttachments === "function") {
        try {
          return (parent.getAttachments() || []).map(getItem).filter(Boolean);
        } catch (error) {
          // Fall back to a library scan if the parent attachment list is unavailable.
        }
      }
    }

    if (typeof Zotero.Items.getAll !== "function") return [];
    try {
      return await Zotero.Items.getAll(original.libraryID) || [];
    } catch (error) {
      return [];
    }
  }

  async function snapshotCandidateIDs(original) {
    var candidateIDs = new Set();
    try {
      if (original.parentItemID) {
        var parent = getItem(original.parentItemID);
        if (parent && typeof parent.getAttachments === "function") {
          addIDs(candidateIDs, parent.getAttachments());
          return candidateIDs;
        }
      }

      if (typeof Zotero.Items.getAll === "function") {
        addIDs(candidateIDs, await Zotero.Items.getAll(original.libraryID));
      }
    } catch (error) {
      // Import should still proceed if a pre-import snapshot cannot be read.
    }
    return candidateIDs;
  }

  async function findReplacementOnce(original, translatedTitle, preexistingIDs, dateAdded) {
    var items = await getScopedItems(original);
    var candidates = [];
    for (var item of items) {
      if (!isMatchingAttachment(item, original, translatedTitle)) continue;
      if (preexistingIDs.has(String(item.id)) || !itemExists(item)) continue;
      candidates.push(item);
    }

    candidates.sort(function (first, second) {
      var firstMatchesDate = dateAdded && first.dateAdded === dateAdded;
      var secondMatchesDate = dateAdded && second.dateAdded === dateAdded;
      if (firstMatchesDate !== secondMatchesDate) return firstMatchesDate ? -1 : 1;
      return 0;
    });
    return candidates[0] || null;
  }

  function waitForReplacementRetry() {
    return new Promise(function (resolve) {
      setTimeout(resolve, REPLACEMENT_RETRY_DELAY_MS);
    });
  }

  async function findReplacement(original, translatedTitle, preexistingIDs, dateAdded) {
    for (var attempt = 0; attempt < MAX_REPLACEMENT_LOOKUPS; attempt++) {
      var replacement = await findReplacementOnce(
        original,
        translatedTitle,
        preexistingIDs,
        dateAdded,
      );
      if (replacement) return replacement;
      if (attempt + 1 < MAX_REPLACEMENT_LOOKUPS) await waitForReplacementRetry();
    }
    return null;
  }

  function markTagWarning(item) {
    if (!item) return;
    try {
      Object.defineProperty(item, TAG_WARNING_PROPERTY, {
        value: true,
        configurable: true,
      });
    } catch (error) {
      try {
        item[TAG_WARNING_PROPERTY] = true;
      } catch (assignmentError) {
        // The visible warning below is still shown if the Zotero item is sealed.
      }
    }
  }

  function hasTagID(tagID) {
    return tagID !== false && tagID != null;
  }

  function tagIDsMatch(databaseID, cachedID) {
    var hasDatabaseID = hasTagID(databaseID);
    var hasCachedID = hasTagID(cachedID);
    return hasDatabaseID === hasCachedID
      && (!hasDatabaseID || String(databaseID) === String(cachedID));
  }

  async function synchronizeTranslatedTagCache() {
    var tagName = attachmentRules.TRANSLATED_ATTACHMENT_TAG;
    var databaseID = await Zotero.DB.valueQueryAsync(
      "SELECT tagID FROM tags WHERE name=?",
      [tagName],
    );
    var cachedID = Zotero.Tags.getID(tagName);
    if (!tagIDsMatch(databaseID, cachedID)) {
      await Zotero.Tags.init();
      databaseID = await Zotero.DB.valueQueryAsync(
        "SELECT tagID FROM tags WHERE name=?",
        [tagName],
      );
      cachedID = Zotero.Tags.getID(tagName);
    }
    return {
      databaseID: databaseID,
      cachedID: cachedID,
      consistent: tagIDsMatch(databaseID, cachedID),
    };
  }

  async function ensureTranslatedTagReady() {
    if (
      !Zotero.DB
      || typeof Zotero.DB.valueQueryAsync !== "function"
      || typeof Zotero.DB.executeTransaction !== "function"
      || !Zotero.Tags
      || typeof Zotero.Tags.getID !== "function"
      || typeof Zotero.Tags.init !== "function"
      || typeof Zotero.Tags.create !== "function"
    ) {
      throw new Error("Zotero tag APIs are unavailable");
    }

    var tagName = attachmentRules.TRANSLATED_ATTACHMENT_TAG;
    var state = await synchronizeTranslatedTagCache();
    if (!state.consistent) throw new Error("Zotero tag cache is inconsistent");

    if (!hasTagID(state.databaseID)) {
      // Commit the tag row separately so a later item save cannot roll it back
      // while leaving Zotero.Tags' in-memory name-to-ID cache stale.
      await Zotero.DB.executeTransaction(async function () {
        await Zotero.Tags.create(tagName);
      });
      state = await synchronizeTranslatedTagCache();
    }

    if (!state.consistent || !hasTagID(state.databaseID)) {
      throw new Error("Translated tag could not be prepared");
    }
  }

  async function localizedMessage(messageID, args) {
    try {
      var mainWindow = typeof Zotero.getMainWindow === "function"
        ? Zotero.getMainWindow()
        : null;
      if (!mainWindow || !mainWindow.document) return messageID;
      if (
        mainWindow.MozXULElement
        && typeof mainWindow.MozXULElement.insertFTLIfNeeded === "function"
      ) {
        mainWindow.MozXULElement.insertFTLIfNeeded("babeldoc-for-zotero.ftl");
      }
      var l10n = mainWindow.document.l10n;
      if (!l10n || typeof l10n.formatValue !== "function") return messageID;
      return await l10n.formatValue(messageID, args) || messageID;
    } catch (error) {
      return messageID;
    }
  }

  async function showTagWarning(title) {
    var headline = await localizedMessage("babeldoc-for-zotero-import-tag-warning-title");
    var attachmentName = await localizedMessage(
      "babeldoc-for-zotero-import-tag-warning-default-name",
    );
    var description = await localizedMessage(
      "babeldoc-for-zotero-import-tag-warning-description",
      { title: String(title || attachmentName) },
    );
    try {
      if (typeof Zotero.ProgressWindow === "function") {
        var progress = new Zotero.ProgressWindow({ closeOnClick: true });
        progress.changeHeadline(headline);
        if (typeof progress.ItemProgress === "function") {
          var itemProgress = new progress.ItemProgress("attachment", attachmentName);
          itemProgress.setText(description);
        } else {
          throw new Error("Plain-text progress item is unavailable");
        }
        progress.show();
        progress.startCloseTimer(5000);
        return;
      }
    } catch (error) {
      // Fall back to an alert if Zotero's notification window is unavailable.
    }

    try {
      if (typeof Zotero.alert === "function") {
        var parentWindow = typeof Zotero.getMainWindow === "function" ? Zotero.getMainWindow() : null;
        Zotero.alert(parentWindow, headline, description);
      }
    } catch (error) {
      // UI reporting must not turn a successful import into a failed translation.
    }
  }

  async function tagLiveAttachment(
    original,
    translatedTitle,
    importedAttachment,
    preexistingIDs,
    tagReady,
  ) {
    var liveAttachment = importedAttachment;
    var importedDateAdded = importedAttachment.dateAdded;

    if (!itemExists(liveAttachment)) {
      liveAttachment = await findReplacement(
        original,
        translatedTitle,
        preexistingIDs,
        importedDateAdded,
      ) || importedAttachment;
      if (liveAttachment === importedAttachment && !itemExists(liveAttachment)) {
        markTagWarning(liveAttachment);
        await showTagWarning(translatedTitle);
        return liveAttachment;
      }
    }

    if (!tagReady) {
      markTagWarning(liveAttachment);
      await showTagWarning(translatedTitle);
      return liveAttachment;
    }

    for (var attempt = 0; attempt < MAX_REPLACEMENT_LOOKUPS; attempt++) {
      try {
        liveAttachment.addTag(attachmentRules.TRANSLATED_ATTACHMENT_TAG, 1);
        await liveAttachment.saveTx();
        return liveAttachment;
      } catch (error) {
        // Rebuild Zotero's tag cache from the database before retrying a
        // replacement, including after a rolled-back itemTags transaction.
        try {
          await ensureTranslatedTagReady();
        } catch (cacheError) {
          // The translation remains usable even if its marker cannot be repaired.
        }
        // Another plugin may have erased this item while its tag transaction was saving.
        if (itemExists(liveAttachment)) break;
        if (attempt + 1 >= MAX_REPLACEMENT_LOOKUPS) break;
        liveAttachment = await findReplacement(
          original,
          translatedTitle,
          preexistingIDs,
          importedDateAdded,
        );
        if (!liveAttachment) {
          liveAttachment = importedAttachment;
          break;
        }
      }
    }

    markTagWarning(liveAttachment);
    await showTagWarning(translatedTitle);
    return liveAttachment;
  }

  async function importResult(original, outputPath, kind) {
    if (!original || !outputPath) throw new Error("缺少原附件或译本 PDF 文件。");
    var outputKind = kind === "dual" ? "dual" : "mono";

    var originalPath = await original.getFilePathAsync();
    if (!originalPath) throw new Error("无法读取原 PDF 文件名。");

    var originalFileName = getOriginalFileName(originalPath);
    var originalTitle = String(original.getField("title") || "");
    var translatedTitle = originalTitle + (outputKind === "dual"
      ? attachmentRules.DUAL_TITLE_SUFFIX
      : attachmentRules.TRANSLATED_TITLE_SUFFIX);
    var parentItemID = original.parentItemID;
    var importOptions = {
      file: outputPath,
      parentItemID: parentItemID ? parentItemID : undefined,
      libraryID: original.libraryID,
      title: translatedTitle,
      contentType: "application/pdf",
      fileBaseName: getTranslatedFileBaseName(originalFileName, outputKind),
    };

    if (!parentItemID) {
      // Zotero accepts collections for standalone attachments, not together with parentItemID.
      importOptions.collections = original.getCollections() || [];
    }

    // Zotero inserts the attachment before importFromFile returns (it then waits for indexing).
    // Reserve its identity during that gap so a parent-item menu click cannot enqueue it.
    var tagReady = false;
    try {
      await ensureTranslatedTagReady();
      tagReady = true;
    } catch (error) {
      // Import the PDF even if Zotero cannot prepare the optional marker tag.
    }
    var endPendingImport = attachmentRules.beginTranslatedImport(original, translatedTitle);
    try {
      var preexistingCandidateIDs = await snapshotCandidateIDs(original);
      // Always import a new attachment, even when the same title already exists under this parent.
      var translatedAttachment = await Zotero.Attachments.importFromFile(importOptions);
      if (!translatedAttachment) throw new Error("Zotero 未能创建译本附件。");
      return await tagLiveAttachment(
        original,
        translatedTitle,
        translatedAttachment,
        preexistingCandidateIDs,
        tagReady,
      );
    } finally {
      endPendingImport();
    }
  }

  return { importResult: importResult };
})();
