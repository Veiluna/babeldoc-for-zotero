/* global Zotero, ZoteroTranslateFiles, ZoteroTranslateFilesAttachments, ZoteroTranslateFilesQueue */

var ZoteroTranslateFilesMenu = (function () {
  var MENU_ID = "zotero-translate-files-translate-pdf";
  var FTL_ID = "zotero-translate-files-menu-label";
  var windows = new Map();

  function selectedItemsForWindow(window) {
    try {
      var pane = window && window.ZoteroPane;
      if (!pane && Zotero.getActiveZoteroPane) pane = Zotero.getActiveZoteroPane();
      return pane && typeof pane.getSelectedItems === "function" ? pane.getSelectedItems() : [];
    } catch (error) {
      return [];
    }
  }

  function resolveIDs(items) {
    return ZoteroTranslateFilesAttachments.resolvePDFAttachments(items).map(function (item) {
      return item.id;
    });
  }

  function commandForItems(items) {
    ZoteroTranslateFilesQueue.enqueue(resolveIDs(items));
  }

  function addDOMMenu(window) {
    var popup = window.document && window.document.querySelector("#zotero-itemmenu");
    if (!popup || typeof window.document.createXULElement !== "function") return null;

    var menuItem = window.document.createXULElement("menuitem");
    menuItem.id = MENU_ID;
    if (window.document.l10n && typeof window.document.l10n.setAttributes === "function") {
      window.document.l10n.setAttributes(menuItem, FTL_ID);
    } else {
      menuItem.setAttribute("label", "翻译成中文 PDF");
    }
    menuItem.hidden = true;

    var onPopupShowing = function () {
      menuItem.hidden = resolveIDs(selectedItemsForWindow(window)).length === 0;
    };
    var onCommand = function () {
      commandForItems(selectedItemsForWindow(window));
    };

    popup.addEventListener("popupshowing", onPopupShowing);
    menuItem.addEventListener("command", onCommand);
    popup.appendChild(menuItem);
    return {
      popup: popup,
      menuItem: menuItem,
      onPopupShowing: onPopupShowing,
      onCommand: onCommand,
    };
  }

  function removeDOMMenu(entry) {
    if (!entry) return;
    entry.popup.removeEventListener("popupshowing", entry.onPopupShowing);
    entry.menuItem.removeEventListener("command", entry.onCommand);
    if (entry.menuItem.parentNode) entry.menuItem.parentNode.removeChild(entry.menuItem);
  }

  return {
    onMainWindowLoad: function (data) {
      var window = data && data.window;
      if (!window || windows.has(window)) return;

      windows.set(window, addDOMMenu(window));
    },

    onMainWindowUnload: function (data) {
      var window = data && data.window;
      if (!window || !windows.has(window)) return;

      removeDOMMenu(windows.get(window));
      windows.delete(window);
    },

    shutdown: function () {
      for (var entry of windows.values()) removeDOMMenu(entry);
      windows.clear();
    },
  };
})();
