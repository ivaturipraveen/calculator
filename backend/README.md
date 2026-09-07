# API

FastAPI over `../calculators`. Read-only; the only writes in this system are
`build_all.py` publishing the specs.

```bash
python3 ../run.py --api-only           # creates the venv on first run
./.venv/bin/python smoke_test.py       # every renderer, over HTTP
```

Interactive docs: <http://127.0.0.1:8010/docs>

## Endpoints

Eight, one job each. No aliases, and nothing that another endpoint already does.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | liveness plus the build date of the specs |
| GET | `/api/catalog` | specialties and types, with counts (drives the filter rail) |
| GET | `/api/calculators/names` | every calculator's name, alphabetically |
| GET | `/api/calculators` | search and filter (`q`, `category`, `renderer`) |
| GET | `/api/calculators/{slug}` | the full spec — formulas, tables, notes, provenance |
| GET | `/api/calculators/{slug}/schema` | fields, validation rules and the **section plan** |
| POST | `/api/calculators/{slug}/calculate` | evaluate; typed response, one call per keystroke |
| POST | `/api/calculators/{name}/run` | ask what it needs, or send values and get the answer |

Every `{slug}` also accepts the printed name — `Dobutamine`, `4T score`, or an
unambiguous fragment like `cockcroft gault`.

### `/calculate` and `/run` are not the same endpoint twice

`/calculate` is what the UI calls on every edit: a small typed body in, a typed
body out, no schema echoed back. `/run` is the one-call flow for a script or a
new client — it answers "what does this need?" and "here is the answer" from the
same path, so a caller loops on one endpoint instead of learning two.

### What was removed, and why

* `/api/names` — the same handler under a second path. An alias is a second
  thing to document and to keep working.
* `/api/units` — every field already carries its own unit list inside
  `/schema`, converted to that field's base. A separate registry was a second
  answer to "what units does this take".
* `/api/{slug}/validate` — `/calculate` already returns the same per-field
  errors. Two endpoints meant two implementations of "is this form valid yet",
  and they could disagree.

## `/run` — the whole interaction in one endpoint

```bash
# what does it need?
curl -s -X POST localhost:8010/api/calculators/Dobutamine/run -d '{}' \
     -H 'content-type: application/json' | jq '.needs.fields[].label'

# answer it — keys or printed labels, flat or nested
curl -s -X POST localhost:8010/api/calculators/Dobutamine/run \
     -H 'content-type: application/json' \
     -d '{"Dose":5,"Weight":70,"Concentration":1000,"Drug Amount":250,"Infusate Volume":250}'
```

`status` is `needs_values` or `answered`, so a caller loops on one endpoint
instead of learning two. Anything it could not match comes back in
`unknown_keys` rather than being silently dropped.

## The section plan

`/schema` returns `sections`: an ordered list of `{ id, kind, ... }`. A client
has one component per `kind` and renders them in order. The kinds in use:

`fields` · `score_groups` · `converter` · `tree` · `results` · `score_result` ·
`titration` · `drug_table` · `threshold_table` · `formula` · `notes` ·
`references` · `legal`

This is what keeps 178 calculators from becoming 178 client components. A new
calculator shape is a new kind here and a new component there — never a branch
on a slug.

## One request shape, one response union

`POST /calculate` accepts `inputs`+`units` (formula, growth chart, infusion),
`selections` (score), `answers` (decision tree), or `pair_index`+`value`
(converter). The response populates whichever of `outputs`, `score`, `table`,
`drugs`, `conversion` or `tree` this calculator produces, and omits the rest.

## Units

Values arrive in whatever unit the form is showing; `units: {field: code}` says
which. The server converts to the field's base unit — `base = raw * factor +
offset`, affine because temperature needs the offset — and **then** checks the
bounds, so a weight typed in pounds is never compared against a limit in
kilograms.

## Evaluation

`extractor.evaluator` is imported, not reimplemented. A second implementation
would be a second set of rounding rules and a second chance to be wrong. It
parses each expression to an AST and walks a whitelist of node types; nothing
scraped from a PDF is ever passed to `eval`.
