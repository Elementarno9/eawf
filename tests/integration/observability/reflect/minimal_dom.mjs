// Execute an emitted page's scripts against a minimal DOM, with no browser engine.
//
// Input (stdin, JSON): {"tree": <element>, "scripts": [{"src": name} | {"code": text}],
// "files": {name: text}}. The tree is the page's parsed markup; a src script is read from
// "files", and a src with no file there is a missing chunk, recorded as such before the
// next script runs, as a browser would.
//
// Output (stdout, JSON): what went wrong -- thrown errors, rejected promises, anything a
// .catch sent to console.error, missing chunks -- and what the page drew once every
// disclosure was opened: headings, table rows and cells, labelled and unlabelled
// controls, and the text of every cell.
import vm from "node:vm";

const input = JSON.parse(await new Promise((resolve) => {
  let data = "";
  process.stdin.on("data", (chunk) => { data += chunk; });
  process.stdin.on("end", () => resolve(data));
}));

class Element {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toUpperCase();
    this.attributes = { ...attrs };
    this.id = attrs.id ?? "";
    this.children = [];
    this.parent = null;
    this.ownText = "";
    this.listeners = {};
    this.open = "open" in attrs;
  }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  get textContent() { return this.ownText + this.children.map((c) => c.textContent).join(""); }
  set textContent(value) { this.children = []; this.ownText = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, handler) { (this.listeners[type] ??= []).push(handler); }
  dispatchEvent(event) { for (const h of this.listeners[event.type] ?? []) h.call(this, event); }
  *walk() { for (const c of this.children) { yield c; yield* c.walk(); } }
  matches(simple) {
    if (simple.startsWith("#")) return this.id === simple.slice(1);
    if (simple.startsWith(".")) return (this.attributes.class ?? "").split(/\s+/).includes(simple.slice(1));
    return this.tagName === simple.toUpperCase();
  }
  querySelectorAll(selector) {
    const parts = selector.trim().split(/\s+/);
    let scope = [this];
    for (const part of parts) {
      scope = scope.flatMap((node) => [...node.walk()].filter((el) => el.matches(part)));
    }
    return [...new Set(scope)];
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
  get rows() { return [...this.walk()].filter((el) => el.tagName === "TR"); }
  get cells() { return this.children.filter((el) => el.tagName === "TD" || el.tagName === "TH"); }
  insertRow() { return this.appendChild(new Element("tr")); }
  insertCell() { return this.appendChild(new Element("td")); }
}

function build(node) {
  const el = new Element(node.tag, node.attrs);
  el.ownText = node.text ?? "";
  for (const child of node.children ?? []) el.appendChild(build(child));
  return el;
}

const root = build(input.tree);
const errors = [];
const missing = [];
const record = (kind, value) => errors.push({ kind, message: String(value?.stack ?? value) });
const document = {
  documentElement: root,
  getElementById: (id) => [...root.walk()].find((el) => el.id === id) ?? null,
  querySelector: (s) => root.querySelector(s),
  querySelectorAll: (s) => root.querySelectorAll(s),
  createElement: (tag) => new Element(tag),
};
const console_ = {
  log: () => {}, info: () => {}, debug: () => {},
  warn: (...args) => record("console.warn", args.join(" ")),
  error: (...args) => record("console.error", args.join(" ")),
};
const context = vm.createContext({ document, console: console_, Promise, JSON, Math, String, Number, Object, Array, Set, Map });
context.window = context;
process.on("unhandledRejection", (reason) => record("rejected", reason));

for (const script of input.scripts) {
  let code = script.code;
  if (script.src !== undefined) {
    code = input.files[script.src];
    if (code === undefined) { missing.push(script.src); continue; }
  }
  try { vm.runInContext(code, context, { filename: script.src ?? "inline" }); }
  catch (error) { record("thrown", error); }
}
await new Promise((resolve) => setTimeout(resolve, 0));

const disclosures = root.querySelectorAll("details");
for (const details of disclosures) {
  details.open = true;
  try { details.dispatchEvent({ type: "toggle", target: details }); }
  catch (error) { record("thrown", error); }
}
await new Promise((resolve) => setTimeout(resolve, 0));

const all = [...root.walk()];
const labelled = new Set(all.filter((el) => el.tagName === "LABEL").map((el) => el.getAttribute("for")));
const controls = all.filter((el) => ["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(el.tagName));
process.stdout.write(JSON.stringify({
  errors,
  missing_chunks: missing,
  disclosures_opened: disclosures.length,
  headings: all.filter((el) => /^H[1-6]$/.test(el.tagName)).map((el) => [el.tagName, el.textContent]),
  unlabelled_controls: controls.filter((el) => !labelled.has(el.id) && !el.getAttribute("aria-label")).length,
  texts: Object.fromEntries(all.filter((el) => el.id).map((el) => [el.id, el.textContent])),
  tables: Object.fromEntries(all.filter((el) => el.tagName === "TABLE" && el.id).map((table) => [
    table.id,
    table.querySelectorAll("tbody tr").map((row) => row.cells.map((cell) => cell.textContent)),
  ])),
}));
