// Executes the report's two inline scripts against the DOM shim and prints a JSON summary.
//   node tests/run_report.js <report.html> <mode> <tierPct> <lang>
"use strict";
const fs = require("fs");
const path = require("path");
const { install } = require(path.join(__dirname, "report_dom_shim.js"));

const [file, mode = "simple", tier = "100", lang = "en"] = process.argv.slice(2);
const html = fs.readFileSync(file, "utf8");
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (blocks.length < 2) { console.error("expected two inline scripts"); process.exit(2); }

const advCount = (html.match(/data-adv/g) || []).length;
const ids = [...html.matchAll(/\bid="([A-Za-z0-9_]+)"/g)].map((m) => m[1]);
const { registry, advNodes } = install({
  advCount, ids,
  store: { "memtier.mode": mode, "memtier.tierPct": tier, "memtier.lang": lang },
});

eval(blocks[0].replace("window.MEMTIER =", "global.window.MEMTIER ="));
eval(blocks[1]);

const text = (n) => !n ? "" : n.nodeType === 3 ? n.textContent
  : (n.children && n.children.length ? n.children.map(text).join(" ") : n.textContent || "");
const rows = (t) => { const out = []; t.children.forEach((sec) => sec.children.forEach((tr) => out.push(tr.children.map(text)))); return out; };

console.log(JSON.stringify({
  mode, tier: +tier, lang,
  ids: ids.length,
  advTotal: advNodes.length,
  advHidden: advNodes.filter((n) => n.hidden).length,
  verdict: text(registry.verdict || null),
  verdictHidden: !!(registry.verdict && registry.verdict.hidden),
  kpis: (registry.kpis ? registry.kpis.children : []).map(text),
  sizing: registry.sizingTable ? rows(registry.sizingTable) : [],
  candidates: (registry.candList ? registry.candList.children : []).length,
}, null, 1));
