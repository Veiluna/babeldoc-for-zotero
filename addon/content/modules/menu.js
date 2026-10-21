/* global Zotero, ZoteroTranslateFiles, ZoteroTranslateFilesAttachments, ZoteroTranslateFilesQueue */

var ZoteroTranslateFilesMenu = (function () {
  var MENU_ID = "babeldoc-for-zotero-menu";
  var ROOT_FTL_ID = "babeldoc-for-zotero-menu-root";
  var ITEMS = [
    {
      id: "babeldoc-for-zotero-translate-pdf",
      ftlID: "babeldoc-for-zotero-menu-mono",
      fallbackLabel: "翻译成中文 PDF",
      outputMode: "mono",
    },
    {
      id: "babeldoc-for-zotero-translate-pdf-dual",
      ftlID: "babeldoc-for-zotero-menu-dual",
      fallbackLabel: "生成双语对照 PDF",
      outputMode: "dual",
    },
  ];
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

  function setLabel(document, element, ftlID, fallbackLabel) {
    if (document.l10n && typeof document.l10n.setAttributes === "function") {
      document.l10n.setAttributes(element, ftlID);
    } else {
      element.setAttribute("label", fallbackLabel);
    }
  }

  function commandForItem(window, outputMode) {
    ZoteroTranslateFilesQueue.enqueue(resolveIDs(selectedItemsForWindow(window)), outputMode);
  }

  function addDOMMenu(window) {
    var document = window.document;
    var popup = document && document.querySelector("#zotero-itemmenu");
    if (!popup || typeof document.createXULElement !== "function") return null;

    var menu = document.createXULElement("menu");
    menu.id = MENU_ID;
    menu.setAttribute("class", "menu-iconic");
    setLabel(document, menu, ROOT_FTL_ID, "BabelDOC for Zotero");
    menu.hidden = true;

    var submenu = document.createXULElement("menupopup");
    menu.appendChild(submenu);

    var items = ITEMS.map(function (definition) {
      var menuItem = document.createXULElement("menuitem");
      menuItem.id = definition.id;
      setLabel(document, menuItem, definition.ftlID, definition.fallbackLabel);
      var onCommand = function () {
        commandForItem(window, definition.outputMode);
      };
      menuItem.addEventListener("command", onCommand);
      submenu.appendChild(menuItem);
      return { menuItem: menuItem, onCommand: onCommand };
    });

    var onPopupShowing = function () {
      menu.hidden = resolveIDs(selectedItemsForWindow(window)).length === 0;
    };

    popup.addEventListener("popupshowing", onPopupShowing);
    popup.appendChild(menu);
    return {
      popup: popup,
      menu: menu,
      items: items,
      onPopupShowing: onPopupShowing,
    };
  }

  function removeDOMMenu(entry) {
    if (!entry) return;
    entry.popup.removeEventListener("popupshowing", entry.onPopupShowing);
    for (var item of entry.items) {
      item.menuItem.removeEventListener("command", item.onCommand);
    }
    if (entry.menu.parentNode) entry.menu.parentNode.removeChild(entry.menu);
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
