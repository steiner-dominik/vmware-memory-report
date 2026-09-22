// Minimal DOM, just enough to execute the report's render pass outside a browser.
// Not a browser: it catches runtime errors and bad values, never layout or CSS.
// Used by tests/test_report_data.py; skipped when node is not installed.
"use strict";

function mkNode(tag) {
  const n = {
    tagName: String(tag || "").toUpperCase(), nodeName: String(tag || "").toUpperCase(),
    children: [], childNodes: [], attrs: {}, dataset: {}, _text: "",
    className: "", clientWidth: 400, disabled: false, hidden: false, value: "", checked: false,
    style: { setProperty() {} },
    classList: {
      add(...names) { for (const c of names) { if (!c) throw new Error("classList.add: empty token"); } },
      remove() {}, toggle() {}, contains() { return false; },
    },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); this.children.length = 0; this.childNodes.length = 0; },
    setAttribute(k, v) { this.attrs[k] = String(v); if (k === "class") this.className = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    appendChild(c) { this.children.push(c); this.childNodes.push(c); return c; },
    removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) { this.children.splice(i, 1); this.childNodes.splice(i, 1); } },
    remove() {}, focus() {}, click() {},
    addEventListener() {}, removeEventListener() {},
    getBoundingClientRect() { return { left: 0, top: 0, right: 100, bottom: 20, width: 400, height: 150 }; },
    querySelectorAll() { return []; },
  };
  Object.defineProperty(n, "title", { get() { return this.attrs.title || ""; }, set(v) { this.attrs.title = v; } });
  return n;
}

function install(opts) {
  const registry = {};
  const advNodes = [];
  for (let i = 0; i < (opts.advCount || 0); i++) advNodes.push(mkNode("section"));
  const stdNodes = [];
  for (let i = 0; i < (opts.stdCount || 0); i++) stdNodes.push(mkNode("section"));
  // Only ids the page really declares exist. A shim that invents nodes on demand hides the
  // exact bug this harness is for: JavaScript still reaching for an element that was removed
  // from the markup. The browser returns null there and the next property access throws.
  const known = new Set(opts.ids || []);
  global.document = {
    documentElement: mkNode("html"),
    body: mkNode("body"),
    title: "",
    getElementById: (id) => {
      if (!known.has(id)) return null;
      return registry[id] || (registry[id] = mkNode("div"));
    },
    createElement: mkNode,
    createElementNS: (ns, tag) => mkNode(tag),
    createTextNode: (txt) => ({ nodeType: 3, textContent: String(txt) }),
    querySelectorAll: (sel) => (sel === "[data-adv]" ? advNodes : sel === "[data-std]" ? stdNodes : []),
    addEventListener() {},
  };
  global.window = { innerWidth: 1400, innerHeight: 900, addEventListener() {}, MEMTIER: null };
  const store = opts.store || {};
  global.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem(k, v) { store[k] = v; } };
  global.Blob = function () {};
  global.URL = { createObjectURL: () => "blob:", revokeObjectURL() {} };
  global.setTimeout = () => 0;
  global.clearTimeout = () => {};
  return { registry, advNodes, stdNodes };
}

module.exports = { install, mkNode };
