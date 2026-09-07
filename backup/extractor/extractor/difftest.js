/**
 * Differential-test harness: run the mockup's OWN calculator function on a set
 * of input vectors and report what it produces.
 *
 * This is the strongest available check on a scraped formula. The Python side
 * evaluates the expression extracted from the PDF's embedded JavaScript; this
 * side runs the hand-written implementation in the mockup. Agreement across
 * random inputs is strong evidence both are faithful to the source; any
 * disagreement is a concrete defect in one of them.
 *
 * Usage: node difftest.js <mockup.html> <job.json>
 *   job.json = { "id": "pdf2",
 *                "stateVar": "pdf2State",
 *                "calcFn": "calcPdf2",
 *                "resultPath": "result",
 *                "vectors": [ { "ageVal": "40", ... }, ... ] }
 */

const fs = require("fs");
const vm = require("vm");

const [, , htmlPath, jobPath] = process.argv;
const html = fs.readFileSync(htmlPath, "utf8");
const job = JSON.parse(fs.readFileSync(jobPath, "utf8"));

const source = html.match(/<script>([\s\S]*)<\/script>/)[1];

function makeStub() {
  const t = function () {};
  return new Proxy(t, {
    get(_x, p) {
      if (p === Symbol.toPrimitive) return () => "";
      if (p === "then") return undefined;
      if (p === "value" || p === "textContent" || p === "innerHTML") return "";
      return makeStub();
    },
    set() { return true; },
    apply() { return makeStub(); },
    construct() { return makeStub(); },
    has() { return true; },
  });
}

const sandbox = {
  // Returning null keeps the innerHTML-shadowing IIFE from installing; the
  // render functions that follow will throw, which we swallow per-call below
  // because the result is assigned to state BEFORE render is invoked.
  document: {
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => makeStub(),
    addEventListener: () => {},
    body: makeStub(),
  },
  window: { addEventListener: () => {}, innerWidth: 1440 },
  alert: (m) => { sandbox.__alerts.push(String(m)); },
  console: { log: () => {}, warn: () => {}, error: () => {} },
  Blob: function () {},
  URL: { createObjectURL: () => "", revokeObjectURL: () => {} },
  setTimeout: () => 0,
  clearTimeout: () => {},
  __alerts: [],
};
sandbox.globalThis = sandbox;
const ctx = vm.createContext(sandbox);

let patched = source.replace(
  /\n\s*(populateCategoryFilter|renderList|renderDetail|updateStatusLine)\(\);?/g,
  "\n/* boot */"
);
patched = patched.replace(/^(const|let)\s+/gm, "var ");
vm.runInContext(patched, ctx, { filename: "mockup.js", timeout: 30000 });

const results = [];
for (const vec of job.vectors) {
  sandbox.__alerts = [];
  let value = null, error = null;

  try {
    // Reset to pristine defaults, then overlay this vector.
    ctx[job.stateVar] = null;
    const ensure = ctx["ensure" + job.stateVar.charAt(0).toUpperCase() + job.stateVar.slice(1)];
    const st = typeof ensure === "function" ? ensure() : null;
    if (st) Object.assign(st, vec);

    try {
      ctx[job.calcFn]();
    } catch (e) {
      // Render functions need a live DOM; the computed result is already
      // committed to state by the time they run, so this is expected.
      error = "render: " + e.message;
    }

    const state = ctx[job.stateVar];
    value = state ? state[job.resultPath ?? "result"] : null;
    if (value !== null && value !== undefined) {
      value = JSON.parse(JSON.stringify(value));
    }
  } catch (e) {
    error = e.message;
  }

  results.push({
    inputs: vec,
    result: value ?? null,
    alerts: sandbox.__alerts.slice(),
    error: value == null ? error : null,
  });
}

process.stdout.write(JSON.stringify({ id: job.id, results }, null, 2));
