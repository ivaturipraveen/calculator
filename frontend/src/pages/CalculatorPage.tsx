import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { CalculatorSchema } from "../api/types";
import { useCalculator } from "../hooks/useCalculator";
import type { CalcState } from "../hooks/useCalculator";
import { INFO_KINDS, SectionRenderer } from "../components/sections/SectionRenderer";
import { InfoPane } from "../components/InfoPane";
import { formatValue } from "../lib/format";

const RENDERER_LABEL: Record<string, string> = {
  formula: "Formula",
  score: "Score",
  lms: "Growth chart",
  convert: "Converter",
  titration_table: "Infusion",
  dose_table: "Dose table",
  mmed: "Opioid equivalence",
  tree: "Decision tree",
};

export function CalculatorPage() {
  // The URL carries the calculator's printed name, not its slug: a link a
  // clinician can read is a link they can check before they open it. The API
  // resolves a name, a slug or an unambiguous fragment to the same spec.
  const { name = "" } = useParams();
  const slug = decodeURIComponent(name);
  const [schema, setSchema] = useState<CalculatorSchema | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const c = new AbortController();
    setSchema(null);
    setError(null);
    api
      .schema(slug, c.signal)
      .then(setSchema)
      .catch((e) => e.name !== "AbortError" && setError(String(e.message ?? e)));
    return () => c.abort();
  }, [slug]);

  if (error) {
    return (
      <main className="detail-pane">
        <div className="detail-inner">
          <div className="notice notice--error" role="alert">
            <div>
              <strong>Could not load {slug}.</strong>
              <div style={{ marginTop: 4 }}>{error}</div>
              <div style={{ marginTop: 10 }}>
                <Link to="/">Back to all calculators</Link>
              </div>
            </div>
          </div>
        </div>
      </main>
    );
  }

  if (!schema) {
    return (
      <main className="detail-pane">
        <div className="detail-inner">
          <div className="skeleton" style={{ height: 18, width: 150, marginBottom: 14 }} />
          <div className="skeleton" style={{ height: 34, width: "min(460px, 80%)" }} />
          <div className="skeleton" style={{ height: 230, marginTop: 24 }} />
          <div className="skeleton" style={{ height: 96, marginTop: 18 }} />
        </div>
      </main>
    );
  }

  return <CalculatorView schema={schema} key={schema.slug} />;
}

function CalculatorView({ schema }: { schema: CalculatorSchema }) {
  const calc = useCalculator(schema);

  // Two columns, two jobs. What the clinician does -- enter values, read the
  // answer -- is the middle. What the source document SAYS -- its formula, its
  // notes, its references, its disclaimer -- is the right, open, all of it, all
  // the time. Folded into `<details>` on the same column it was material nobody
  // opened, which for a disclaimer is the same as not carrying it.
  const work = schema.sections.filter((s) => !INFO_KINDS.has(s.kind));
  const reference = schema.sections.filter((s) => INFO_KINDS.has(s.kind));
  const general = schema.help?.general ?? [];
  const headline =
    calc.result?.score != null
      ? { label: calc.result.score.band?.label ?? "Total", value: String(calc.result.score.total) }
      : calc.result?.outputs?.[0]
        ? {
            label: calc.result.outputs[0].label,
            value: `${formatValue(calc.result.outputs[0])}${
              calc.result.outputs[0].unit ? ` ${calc.result.outputs[0].unit}` : ""
            }`,
          }
        : null;

  return (
    <>
      <main className="detail-pane">
        <div className="detail-inner">
          <div className="eyebrow-row">
            {schema.category && <span className="eyebrow">{schema.category}</span>}
            <span className="eyebrow eyebrow--type">
              {RENDERER_LABEL[schema.renderer] ?? schema.renderer}
            </span>
            {calc.pending && (
              <span className="eyebrow eyebrow--live">
                calculating <span className="spinner-dot" />
              </span>
            )}
          </div>

          <h1>{schema.title}</h1>
          {schema.subtitle && <p className="subtitle">{schema.subtitle}</p>}
          {schema.source && (
            <p className="source-line">
              Extracted from <span>{schema.source.replace(/\.pdf$/i, "")}</span>
            </p>
          )}

          {general.length > 0 && (
            <div className="notice notice--info">
              <ul>
                {general.map((g) => (
                  <li key={g}>{g}</li>
                ))}
              </ul>
            </div>
          )}

          {schema.caveats?.length ? (
            <div className="notice notice--warn" role="note">
              <div>
                <strong>Known limits of this extraction</strong>
                <ul style={{ marginTop: 6 }}>
                  {schema.caveats.map((c) => (
                    <li key={c.kind + (c.field ?? "")}>{c.detail}</li>
                  ))}
                </ul>
              </div>
            </div>
          ) : null}

          {calc.result?.warnings?.length ? (
            <div className="notice notice--warn" role="status">
              <ul>
                {calc.result.warnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {calc.errorsByField["_"] && (
            <div className="notice notice--error" role="alert">
              {calc.errorsByField["_"]}
            </div>
          )}

          {work.map((s) => (
            <div id={s.id} key={s.id}>
              <SectionRenderer section={s} calc={calc} />
            </div>
          ))}

          <div className="detail-actions">
            <button className="btn-reset" onClick={calc.reset}>
              ↺ Reset
            </button>
            {headline && <CopyResult schema={schema} calc={calc} />}
          </div>

          <ValuesUsed schema={schema} calc={calc} />
        </div>
      </main>

      <InfoPane title="Formula & references">
        {reference.length === 0 ? (
          <p className="info-empty">
            The source document carries no formula or notes for this calculator.
          </p>
        ) : (
          reference.map((s) => <SectionRenderer key={s.id} section={s} calc={calc} />)
        )}
      </InfoPane>

      {headline && (
        <div className="mobile-summary" role="status" aria-live="polite">
          <span className="mobile-summary__label">{headline.label}</span>
          <span className="mobile-summary__value">{headline.value}</span>
        </div>
      )}
    </>
  );
}

/**
 * Put the answer, and what produced it, on the clipboard.
 *
 * A result is copied into a note far more often than it is read and retyped,
 * and a number alone is not safe to paste into a chart -- so the inputs and the
 * calculator's name go with it.
 */
function CopyResult({ schema, calc }: { schema: CalculatorSchema; calc: CalcState }) {
  const [done, setDone] = useState(false);

  const copy = async () => {
    const lines = [schema.title];
    for (const f of schema.fields) {
      const v = calc.values[f.key];
      if (v) lines.push(`  ${f.label}: ${v}${calc.units[f.key] ? ` ${calc.units[f.key]}` : ""}`);
    }
    for (const o of calc.result?.outputs ?? []) {
      lines.push(`  ${o.label} = ${formatValue(o)}${o.unit ? ` ${o.unit}` : ""}`);
    }
    if (calc.result?.score) {
      lines.push(`  Total = ${calc.result.score.total} ${calc.result.score.unit}`);
      if (calc.result.score.band) lines.push(`  ${calc.result.score.band.label}`);
    }
    try {
      await navigator.clipboard.writeText(lines.join("\n"));
      setDone(true);
      window.setTimeout(() => setDone(false), 1600);
    } catch {
      // A browser can refuse clipboard access; the result is still on screen.
    }
  };

  return (
    <button className={done ? "btn-reset is-done" : "btn-reset"} onClick={copy}>
      {done ? "✓ Copied" : "Copy result"}
    </button>
  );
}

/**
 * The values the answer was worked out from, beside the answer.
 *
 * A number on its own is not checkable. Reading it back -- "70 kg, 24 mmHg" --
 * is how a typo is caught before it is acted on.
 */
function ValuesUsed({ schema, calc }: { schema: CalculatorSchema; calc: CalcState }) {
  const used = schema.fields
    .map((f) => ({ f, v: (calc.values[f.key] ?? "").trim() }))
    .filter(({ v }) => v !== "");
  if (!used.length || !calc.result?.ok) return null;

  return (
    <details className="used">
      <summary>
        {used.length} value{used.length === 1 ? "" : "s"} used
      </summary>
      <dl className="used__list">
        {used.map(({ f, v }) => {
          const chosen = f.options.find((o) => String(o.value) === v);
          return (
            <div className="used__row" key={f.key}>
              <dt>{f.label}</dt>
              <dd>
                {chosen ? (chosen.label ?? v) : v}
                {!chosen && (calc.units[f.key] || f.unit.display) ? (
                  <span className="used__unit">{calc.units[f.key] || f.unit.display}</span>
                ) : null}
              </dd>
            </div>
          );
        })}
      </dl>
    </details>
  );
}
