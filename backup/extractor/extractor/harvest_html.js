/**
 * Harvest ground truth out of the InpharmD mockup HTML.
 *
 * Rather than regexing the source, we EXECUTE it inside a vm sandbox behind a
 * permissive DOM stub and then read the resulting globals. That gives us the
 * real evaluated values -- unit option lists, conversion factor maps, per-field
 * defaults from each `ensure<Id>State()` -- instead of a best-effort text match.
 *
 * The mockup is the only source for unit dropdown OPTION LISTS: the Lexicomp
 * print view renders every <select> collapsed, so the PDFs carry the conversion
 * *mechanism* but not the option values.
 *
 * Usage: node harvest_html.js <mockup.html> <out.json>
 */

const fs = require("fs");
const vm = require("vm");

const [, , htmlPath, outPath] = process.argv;
if (!htmlPath || !outPath) {
  console.error("usage: node harvest_html.js <mockup.html> <out.json>");
  process.exit(1);
}

const html = fs.readFileSync(htmlPath, "utf8");
const m = html.match(/<script>([\s\S]*)<\/script>/);
if (!m) {
  console.error("no <script> block found");
  process.exit(1);
}
const source = m[1];

/** A stub that absorbs any property access, assignment or call. */
function makeStub() {
  const target = function () {};
  return new Proxy(target, {
    get(_t, prop) {
      if (prop === Symbol.toPrimitive) return () => "";
      if (prop === "then") return undefined; // never look thenable
      if (prop === "length") return 0;
      if (prop === "value" || prop === "textContent" || prop === "innerHTML") return "";
      if (prop === "classList") return makeStub();
      if (prop === "style") return makeStub();
      if (prop === "dataset") return makeStub();
      return makeStub();
    },
    set() { return true; },
    apply() { return makeStub(); },
    construct() { return makeStub(); },
    has() { return true; },
  });
}

const documentStub = {
  getElementById: () => null,     // makes the innerHTML-shadow IIFE bail out early
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => makeStub(),
  addEventListener: () => {},
  body: makeStub(),
};

const sandbox = {
  document: documentStub,
  window: { addEventListener: () => {}, innerWidth: 1440 },
  alert: () => {},
  console: { log: () => {}, warn: () => {}, error: () => {} },
  Blob: function () {},
  URL: { createObjectURL: () => "", revokeObjectURL: () => {} },
  setTimeout: () => 0,
  clearTimeout: () => {},
};
sandbox.globalThis = sandbox;

const context = vm.createContext(sandbox);

// The tail of the script boots the UI against a real DOM; neutralise those calls.
let patched = source.replace(
  /\n\s*(populateCategoryFilter|renderList|renderDetail|updateStatusLine)\(\);?/g,
  "\n/* boot call removed by harvester */"
);

// `const`/`let` are lexically scoped and never become properties of the vm
// context, so top-level declarations would be invisible to the harvest below.
// Rewriting the column-0 ones to `var` attaches them to the sandbox global.
// (Nested declarations are always indented in this file, so anchoring at the
// start of a line is safe.)
patched = patched.replace(/^(const|let)\s+/gm, "var ");

try {
  vm.runInContext(patched, context, { filename: "mockup.js", timeout: 30000 });
} catch (err) {
  console.error("script execution failed:", err.message);
  process.exit(1);
}

/* ------------------------------------------------------------------ */

const out = {
  bespoke: context.BESPOKE ?? null,
  bespoke_info: context.BESPOKE_INFO ?? null,
  new_calcs: context.NEW_CALCS ?? null,
  unit_families: context.UNIT_FAMILIES ?? null,
  per_calculator: {},
  unit_constants: {},
};

/** Globals that look like unit option lists or conversion factor maps. */
const UNITISH = /(_UNITS|_TO_[A-Z0-9_]+|_UNIT_LIST)$/;
for (const key of Object.keys(context)) {
  if (!UNITISH.test(key)) continue;
  const v = context[key];
  if (v && (Array.isArray(v) || typeof v === "object")) out.unit_constants[key] = v;
}

/** Per-bespoke-calculator: its constants, plus the defaults its state factory ships. */
for (const b of out.bespoke ?? []) {
  const id = b.id;
  const prefix = id.toUpperCase() + "_";
  const entry = { id, title: b.n, category: b.cat, constants: {}, defaults: null };

  // `PDF2_AGE_UNITS` matches the prefix, but the big data tables are named
  // without the trailing underscore (`ALS_ADULT`, `RABIES_TREE`, `CDC_TABLES`),
  // so an exact/stem match is needed too or those calculators harvest nothing.
  const bare = id.toUpperCase();
  const stem = bare.replace(/^PDF/, "");
  for (const key of Object.keys(context)) {
    const isPrefixed = key.startsWith(prefix);
    const isBare = key === bare || key.startsWith(bare + "S");
    if (!isPrefixed && !isBare) continue;
    const v = context[key];
    const t = typeof v;
    if (v !== null && (t === "object" || t === "number" || t === "string")) {
      entry.constants[key] = v;
    }
  }

  // Capture the source text of every function belonging to this calculator, so
  // the diff can ask "does this implementation actually enforce bound X?"
  // rather than guessing from evaluated constants (conversion factors and
  // validation bounds are indistinguishable once they are just numbers).
  entry.sources = {};
  const camelId = id.replace(/_([a-z0-9])/g, (_, c) => c.toUpperCase());
  for (const key of Object.keys(context)) {
    if (typeof context[key] !== "function") continue;
    const k = key.toLowerCase();
    if (k.includes(id.toLowerCase()) || k.includes(camelId.toLowerCase())) {
      try { entry.sources[key] = String(context[key]); } catch (e) { /* ignore */ }
    }
  }

  // `ensurePdf2State()` / `ensureAbwState()` ... return the field defaults.
  const camel = id.replace(/_([a-z0-9])/g, (_, c) => c.toUpperCase());
  for (const fname of [
    "ensure" + camel.charAt(0).toUpperCase() + camel.slice(1) + "State",
    "ensure" + id.charAt(0).toUpperCase() + id.slice(1) + "State",
  ]) {
    const fn = context[fname];
    if (typeof fn === "function") {
      try {
        // reset the module-level cache so we observe a pristine state object
        const stateVar = id + "State";
        if (stateVar in context) context[stateVar] = null;
        const st = fn();
        if (st && typeof st === "object") {
          entry.defaults = JSON.parse(
            JSON.stringify(st, (k, val) => (typeof val === "function" ? undefined : val))
          );
        }
      } catch (e) {
        entry.defaults_error = e.message;
      }
      break;
    }
  }
  out.per_calculator[id] = entry;
}

/** Named data tables the bespoke renderers rely on, captured by exact name. */
const DATA_GLOBALS = [
  "ALS_ADULT", "ALS_NEONATAL", "ALS_PEDIATRIC", "ALS_TABLES", "ALS_TITLES",
  "ALS_DEFAULT_WT", "RABIES_TREE", "RABIES_ENDS", "CDC_TABLES", "CDC_CALCS",
  "MMED_DRUGS", "ADMIT_DX_STRUCT", "RSI_ADULT_SECTIONS", "PDF146_SECTIONS",
  "PDF122_DATA", "PDF162_DOSE_UNITS", "PDF164_ADDITIVES",
];
out.data_globals = {};
for (const name of DATA_GLOBALS) {
  if (context[name] !== undefined) out.data_globals[name] = context[name];
}

fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
console.error(
  `harvested: ${out.bespoke?.length ?? 0} bespoke, ` +
    `${Object.keys(out.unit_constants).length} unit constants, ` +
    `${(out.new_calcs?.scores?.length ?? 0) +
       (out.new_calcs?.formulas?.length ?? 0) +
       (out.new_calcs?.converts?.length ?? 0)} generic calculators`
);
