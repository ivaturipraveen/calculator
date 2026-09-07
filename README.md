# Calculator — clinical calculators

178 clinical calculators extracted from their source PDFs, served by an API and
rendered by a web app that has no per-calculator code in it.

```
calculators/     178 JSON specs  ← the artefact the API serves
backend/         FastAPI: catalogue, field schema, validation, evaluation
frontend/        React + TypeScript client
backup/          extractor, source PDFs, mockup, reports — everything that
                 PRODUCES the specs, kept out of the serving path
run.py           starts the backend and the frontend together
```

## Run it

```bash
python3 run.py              # API :8010, web :5173 — installs deps on first run
python3 run.py --build      # rebuild the specs from the PDFs first
python3 run.py --api-only
```

## The flow, end to end

```
  backup/source-pdfs/*.pdf
        │  build_all.py — parse the JavaScript out of each PDF, resolve its
        │  formulas, tables, bounds, options and printed text
        ▼
  backup/extractor/dist/           full build + every audit report
        │  publish (a copy; the release step)
        ▼
  calculators/*.json               ← the only clinical content in the system
        │  read once at startup, indexed by slug and by name
        ▼
  backend/  ── GET  /schema     → fields + validation rules + SECTION PLAN
            ── POST /validate   → per-field errors, values in base units
            ── POST /calculate  → evaluate the spec's own expressions
        │
        ▼
  frontend/ renders the section plan; shows what /calculate returns
```

**Where the formulas are.** In the spec, as text, under `compute`:

```json
"compute": {
  "steps":   [ { "key": "concentration_calc", "expr": "…" } ],
  "outputs": [ { "key": "infuse_rate", "expr": "dose * weight / concentration_calc * 60 / 1000" } ]
}
```

Lookup ladders (growth charts) are in `tables.lookups`, score criteria in
`scoring.groups`, conversion pairs in `tables.pairs`, decision-tree nodes in
`tables.nodes`. The printed formula, as the source document words it, is in
`content.equation`.

**Where validation happens.** Twice, on purpose:

| Where | What it checks |
|---|---|
| Browser | type, `min`/`max`/`step` on the input element — **converted into the unit the box is set to**, so a temperature in °F is not measured against a limit in °C |
| Server, `engine.validate_inputs` | required (and `required_when` — a field the chosen path never reads is not demanded), numeric, option membership, then **unit conversion, then bounds** — a weight typed in pounds is compared against a limit in kilograms only after it has become kilograms |

The browser's copy is a convenience. The server's is the one that decides,
which is why the same rules hold for the mobile app and for `curl`.

Every limit comes from the source document: `minMaxCheck`, the constants a
script declares (`AUC_MIN = 1.5`), and the guards inside `checkInputRanges()`,
including the strict ones — `if (weight <= 0)` becomes "must be greater than 0",
not "at least 0". Limits the script applies to a value it *works out* rather
than to a field — esmolol's 0.1 mL bolus floor, amiodarone's 2.1 g/day ceiling —
are carried in `content.stated_limits` and shown on the page.

**What the page shows beyond the answer.** The section plan carries the
document's own material — the printed formula, its notes, its references, its
disclaimer — plus what the extraction could not recover, stated where it
matters: a critical caveat at the top of the page, `limited` on the catalogue
card, and `content.stated_limits` for a bound the script applies to a value it
works out rather than to a field.

**Where calculation happens.** Server-side, in `extractor.evaluator` — imported,
not reimplemented. It parses each expression to an AST and walks a whitelist of
node types; nothing scraped from a PDF is ever passed to `eval`. The client does
no arithmetic at all.

## Why JSON files and not a database

A spec is a build artefact, not a row anyone edits. Its source of truth is the
PDF plus the extractor, so a database would become a second place the clinical
content could change without a rebuild — and no audit of the PDF would catch
it. 178 specs are ~30 MB, every read is by slug or a whole-catalogue filter, and
a "migration" is re-running `build_all.py`. Put them behind a database at the
point they need per-tenant overrides or an edit history; the shape here is
already the shape a row would take.

## Checks

| Command | What it proves |
|---|---|
| `backup/extractor/validate.py` | all 178 specs evaluate |
| `backup/extractor/invariants.py` | no dead selector, no concatenated lookup ladder, no impossible bounds |
| `backup/extractor/clinical_checks.py` | the numbers match published reference values |
| `backup/extractor/verify_published.py` | `calculators/` matches the build, and every spec is complete |
| `backup/extractor/coverage_audit.py` | what the PDFs say vs what the specs carry |
| `backup/extractor/reconcile.py` | **field by field, one calculator at a time**: the printed form, the bounds, the options, the units, the tables, the score and the constants, each against its own PDF |
| `backend/smoke_test.py` | every renderer works over HTTP |
| `cd frontend && npm test` | the pages a clinician sees show those same numbers |

Run all of them after touching the extractor. `reconcile.py` writes one report
per calculator to `backup/extractor/dist/reports/reconcile/<slug>.json`, so a
single calculator can be read on its own:

```bash
python3 backup/extractor/reconcile.py --only esmolol --failing
```

## Not a medical device

Every calculator carries the source document's own disclaimer, and the UI shows
it. Results are only as good as the extraction; `backup/extractor/dist/reports/`
records what is still uncertain, per calculator.
