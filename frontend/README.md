# Web

React + TypeScript + Vite. Renders any calculator from the API's section plan.

```bash
npm install
npm run dev       # http://localhost:5173
npm test          # mounts real pages against the API on :8010
npm run build
```

`VITE_API_BASE` (see `.env.example`) points at the API; it defaults to
`http://127.0.0.1:8000`.

## How a page is built

`CalculatorPage` fetches `/schema`, hands the sections to `SectionRenderer`, and
that maps `kind` → component. There is no `if (slug === ...)` in the codebase.
Sections whose kind is in `RESULT_KINDS` go to the sticky answer column; the
rest form the page.

```
sections/InputSections.tsx     fields, score criteria, converter, decision tree
sections/ResultSections.tsx    results, score, titration table, drugs, thresholds
sections/ContentSections.tsx   formula, notes, references, disclaimer
```

## Where the arithmetic is

On the server. The client holds the form and renders the answer; every value it
shows came from `/calculate`. Calls are debounced and superseded requests are
aborted, so a fast typist cannot make a stale answer arrive last and win.

## Responsiveness

One column below 1080px with the answer following the form, and a sticky bar
that keeps the headline number in view while the form is scrolled. Below 620px
the filter rail becomes a horizontal strip and every field is full width. Wide
tables scroll inside their own container, never the page.

Themes follow the system by default; the toggle cycles system → light → dark and
remembers the choice.
