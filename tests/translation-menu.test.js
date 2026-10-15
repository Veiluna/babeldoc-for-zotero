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

function makeEnvironment({ menuManager } = {}) {
  const pdf = makeItem(1, { pdf: true, stored: true });
  const parent = makeItem(10, { regular: true, attachmentIDs: [1] });
  const note = { isNote: () => true };
  const enqueued = [];
  const unregistered = [];
  const context = {
    Zotero: {
      Items: { get: id => id === 1 ? pdf : null },
      MenuManager: menuManager,
      getActiveZoteroPane: () => null,
      debug() {},
    },
    ZoteroTranslateFiles: { log() {} },
    ZoteroTranslateFilesQueue: { enqueue: ids => enqueued.push(ids) },
  };
  vm.runInNewContext(attachmentsSource, context, { filename: "attachments.js" });
  vm.runInNewContext(menuSource, context, { filename: "menu.js" });

  return {
    context,
    pdf,
    parent,
    note,
    enqueued,
    unregistered,
  };
}

function managerMenuLifecycleAndVisibility() {
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
  const menu = environment.context.ZoteroTranslateFilesMenu;
  const popupListeners = new Map();
  const children = [];
  const menuListeners = new Map();
  const popup = {
    addEventListener: (name, callback) => popupListeners.set(name, callback),
    removeEventListener: (name, callback) => {
      if (popupListeners.get(name) === callback) popupListeners.delete(name);
    },
    appendChild: child => {
      child.parentNode = popup;
      children.push(child);
    },
    removeChild: child => {
      const index = children.indexOf(child);
      if (index >= 0) children.splice(index, 1);
      child.parentNode = null;
    },
  };
  let selection = [environment.note];
  const window = {
    ZoteroPane: { getSelectedItems: () => selection },
    document: {
      querySelector: () => popup,
      createXULElement: () => ({
        setAttribute() {},
        addEventListener: (name, callback) => menuListeners.set(name, callback),
        removeEventListener: (name, callback) => {
          if (menuListeners.get(name) === callback) menuListeners.delete(name);
        },
      }),
      l10n: { setAttributes() {} },
    },
  };

  menu.onMainWindowLoad({ window });
  assert.equal(registrations, 0, "the menu uses the DOM path even when MenuManager is available");
  assert.equal(children.length, 1, "the DOM menu node must be created");
  const menuItem = children[0];
  popupListeners.get("popupshowing")();
  assert.equal(menuItem.hidden, true, "notes must not show the PDF translation action");
  selection = [environment.parent];
  popupListeners.get("popupshowing")();
  assert.equal(menuItem.hidden, false, "a regular item with a stored PDF must show the action");
  menuListeners.get("command")();
  assert.deepEqual(Array.from(environment.enqueued, ids => Array.from(ids)), [[1]]);

  menu.shutdown();
  assert.equal(children.length, 0, "shutdown must remove the generated menu node");
  assert.equal(popupListeners.size, 0, "shutdown must remove the popup listener");
  assert.equal(menuListeners.size, 0, "shutdown must remove the command listener");
  assert.equal(unregistrations, 0, "the unused manager registration must not be unregistered later");
}

function domFallbackLifecycleAndVisibility() {
  const environment = makeEnvironment();
  const popupListeners = new Map();
  const children = [];
  const menuListeners = new Map();
  const popup = {
    addEventListener: (name, callback) => popupListeners.set(name, callback),
    removeEventListener: (name, callback) => {
      if (popupListeners.get(name) === callback) popupListeners.delete(name);
    },
    appendChild: child => {
      child.parentNode = popup;
      children.push(child);
    },
    removeChild: child => {
      const index = children.indexOf(child);
      if (index >= 0) children.splice(index, 1);
      child.parentNode = null;
    },
  };
  let selection = [environment.note];
  const window = {
    ZoteroPane: { getSelectedItems: () => selection },
    document: {
      querySelector: () => popup,
      createXULElement: () => ({
        setAttribute() {},
        addEventListener: (name, callback) => menuListeners.set(name, callback),
        removeEventListener: (name, callback) => {
          if (menuListeners.get(name) === callback) menuListeners.delete(name);
        },
      }),
      l10n: { setAttributes() {} },
    },
  };
  const menu = environment.context.ZoteroTranslateFilesMenu;
  menu.onMainWindowLoad({ window });

  const menuItem = children[0];
  assert.ok(menuItem.hidden, "the DOM menu must start hidden for a note selection");
  popupListeners.get("popupshowing")();
  assert.ok(menuItem.hidden);

  selection = [environment.parent];
  popupListeners.get("popupshowing")();
  assert.equal(menuItem.hidden, false, "the menu must become visible for a regular item with a PDF");
  menuListeners.get("command")();
  assert.deepEqual(Array.from(environment.enqueued, ids => Array.from(ids)), [[1]]);

  menu.onMainWindowUnload({ window });
  assert.equal(children.length, 0, "unload must remove the injected menu item");
  assert.equal(popupListeners.size, 0, "unload must remove the popup listener");
  assert.equal(menuListeners.size, 0, "unload must remove the command listener");

  menu.onMainWindowLoad({ window });
  assert.equal(children.length, 1, "a later main window load must re-add the DOM menu");
  menu.shutdown();
  assert.equal(children.length, 0, "shutdown must remove the injected menu item");
  assert.equal(popupListeners.size, 0, "shutdown must release the popup listener");
}

managerMenuLifecycleAndVisibility();
domFallbackLifecycleAndVisibility();
process.stdout.write("translation menu regression cases passed\n");
