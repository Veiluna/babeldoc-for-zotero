const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const attachmentsSource = fs.readFileSync(
  path.join(__dirname, "../addon/content/modules/attachments.js"),
  "utf8"
);
const menuSource = fs.readFileSync(
  path.join(__dirname, "../addon/content/modules/menu.js"),
  "utf8"
);

function makeItem(id, { regular = false, pdf = false, stored = false, attachmentIDs = [] } = {}) {
  return {
    id,
    getField: () => "paper.pdf",
    getTags: () => [],
    isPDFAttachment: () => pdf,
    isStoredFileAttachment: () => stored,
    isLinkedFileAttachment: () => false,
    isRegularItem: () => regular,
    getAttachments: () => attachmentIDs,
  };
}

function makeElement(tagName) {
  const listeners = new Map();
  const element = {
    tagName,
    id: "",
    hidden: false,
    attributes: new Map(),
    children: [],
    parentNode: null,
    setAttribute(name, value) {
      this.attributes.set(String(name), String(value));
    },
    getAttribute(name) {
      return this.attributes.get(String(name));
    },
    addEventListener(name, callback) {
      listeners.set(name, callback);
    },
    removeEventListener(name, callback) {
      if (listeners.get(name) === callback) listeners.delete(name);
    },
    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
    },
    removeChild(child) {
      const index = this.children.indexOf(child);
      if (index >= 0) this.children.splice(index, 1);
      child.parentNode = null;
    },
    dispatch(name) {
      const callback = listeners.get(name);
      assert.equal(typeof callback, "function", `missing ${name} listener on ${tagName}`);
      callback({ type: name });
    },
    listenerCount(name) {
      return listeners.has(name) ? 1 : 0;
    },
  };
  return element;
}

function makeEnvironment({ menuManager } = {}) {
  const pdf = makeItem(1, { pdf: true, stored: true });
  const parent = makeItem(10, { regular: true, attachmentIDs: [1] });
  const note = { isNote: () => true };
  const enqueued = [];
  const localized = new Map();
  const popup = makeElement("menupopup");
  const document = {
    querySelector: selector => selector === "#zotero-itemmenu" ? popup : null,
    createXULElement: tagName => makeElement(tagName),
    l10n: {
      setAttributes(element, ftlID) {
        localized.set(element, ftlID);
        element.setAttribute("data-l10n-id", ftlID);
      },
    },
  };
  const context = {
    Zotero: {
      Items: { get: id => id === 1 ? pdf : null },
      MenuManager: menuManager,
      getActiveZoteroPane: () => null,
      debug() {},
    },
    ZoteroTranslateFiles: { log() {} },
    ZoteroTranslateFilesQueue: {
      enqueue(ids, outputMode) {
        enqueued.push({ ids: Array.from(ids), outputMode });
      },
    },
  };
  vm.runInNewContext(attachmentsSource, context, { filename: "attachments.js" });
  vm.runInNewContext(menuSource, context, { filename: "menu.js" });

  return {
    context,
    pdf,
    parent,
    note,
    enqueued,
    popup,
    localized,
    document,
  };
}

function menuLifecycleAndOutputModes() {
  let registrations = 0;
  let unregistrations = 0;
  const environment = makeEnvironment({
    menuManager: {
      registerMenu() {
        registrations++;
        return "registered-menu-id";
      },
      unregisterMenu() { unregistrations++; },
    },
  });
  const menuModule = environment.context.ZoteroTranslateFilesMenu;
  let selection = [environment.note];
  const window = {
    document: environment.document,
    ZoteroPane: { getSelectedItems: () => selection },
  };

  menuModule.onMainWindowLoad({ window });
  assert.equal(registrations, 0, "the menu uses the DOM submenu even when MenuManager is available");
  assert.equal(environment.popup.children.length, 1, "the item menu gets one submenu");

  const menu = environment.popup.children[0];
  assert.equal(menu.tagName, "menu");
  assert.equal(menu.id, "babeldoc-for-zotero-menu");
  assert.equal(menu.getAttribute("class"), "menu-iconic");
  assert.equal(environment.localized.get(menu), "babeldoc-for-zotero-menu-root");
  assert.equal(menu.hidden, true, "the submenu starts hidden for a note selection");
  assert.equal(menu.children.length, 1, "the submenu contains one menupopup");
  const submenu = menu.children[0];
  assert.equal(submenu.tagName, "menupopup");
  assert.equal(submenu.children.length, 2, "the submenu contains both output choices");

  const [monoItem, dualItem] = submenu.children;
  assert.equal(monoItem.tagName, "menuitem");
  assert.equal(monoItem.id, "babeldoc-for-zotero-translate-pdf");
  assert.equal(environment.localized.get(monoItem), "babeldoc-for-zotero-menu-mono");
  assert.equal(dualItem.tagName, "menuitem");
  assert.equal(dualItem.id, "babeldoc-for-zotero-translate-pdf-dual");
  assert.equal(environment.localized.get(dualItem), "babeldoc-for-zotero-menu-dual");

  environment.popup.dispatch("popupshowing");
  assert.equal(menu.hidden, true, "notes must not show the translation submenu");
  selection = [environment.parent];
  environment.popup.dispatch("popupshowing");
  assert.equal(menu.hidden, false, "a regular item with a stored PDF shows the submenu");

  monoItem.dispatch("command");
  dualItem.dispatch("command");
  assert.deepEqual(environment.enqueued, [
    { ids: [1], outputMode: "mono" },
    { ids: [1], outputMode: "dual" },
  ]);

  menuModule.onMainWindowUnload({ window });
  assert.equal(environment.popup.children.length, 0, "unload must remove the whole submenu");
  assert.equal(environment.popup.listenerCount("popupshowing"), 0, "unload must remove the popup listener");
  assert.equal(monoItem.listenerCount("command"), 0, "unload must remove the mono command listener");
  assert.equal(dualItem.listenerCount("command"), 0, "unload must remove the dual command listener");
  assert.equal(unregistrations, 0, "the unused manager registration must not be unregistered later");

  menuModule.onMainWindowLoad({ window });
  assert.equal(environment.popup.children.length, 1, "a later main window load must re-add the submenu");
  menuModule.shutdown();
  assert.equal(environment.popup.children.length, 0, "shutdown must remove the submenu");
  assert.equal(environment.popup.listenerCount("popupshowing"), 0, "shutdown must release the popup listener");
}

menuLifecycleAndOutputModes();
process.stdout.write("translation menu regression cases passed\n");
