import { useState } from "react";
import type { Section } from "../../api/types";
import type { CalcState } from "../../hooks/useCalculator";
import { bandRange, bandTone, formatNumber, formatValue, titleCase } from "../../lib/format";

/** `epoch_ms` is how the arithmetic carries a date, not a unit to print. */
const isDate = (v: { kind?: string; unit?: string | null }) =>
  v.kind === "date" || v.unit === "epoch_ms";
import { Panel } from "../Panel";
import { isHidden } from "../../lib/visibility";

export function ResultsSection({
  section,
  calc,
}: {
  section: Extract<Section, { kind: "results" }>;
  calc: CalcState;
}) {
  const byKey = new Map((calc.result?.outputs ?? []).map((o) => [o.key, o]));

  // A calculator that produces a PANEL of values has no headline. The
  // aminoglycoside work-up hands back an ideal body weight, a dosing weight, a
  // clearance, an elimination constant, a half-life and a volume of
  // distribution: six things a clinician reads together, none of them the
  // answer. Picking one to enlarge and demoting the rest to grey rows buries
  // five of the six. Above three, they are all cards.
  if (section.outputs.length > 3) {
    return (
      <div className="result-cards">
        {section.outputs.map((o) => {
          const got = byKey.get(o.key);
          return (
            <div
              key={o.key}
              className={[
                "result-card",
                got ? "" : "result-card--pending",
                got && calc.stale ? "result-card--stale" : "",
              ].filter(Boolean).join(" ")}
            >
              <span className="result-card__label">{o.label}</span>
              <span className="result-card__value">
                {got ? formatValue(got) : "—"}
                {got && o.unit && !isDate(got) && (
                  <span className="result-card__unit">{o.unit}</span>
                )}
              </span>
            </div>
          );
        })}
        {calc.stale && (
          <p className="result-cards__stale">
            These were worked out from earlier values.
          </p>
        )}
      </div>
    );
  }

  // Three or fewer: boxes of the same shape as the inputs, in the same grid.
  // The reference design reads a calculator as one form -- what you enter, the
  // button, what comes back -- rather than as a form and then a separate
  // coloured card, and it lines the answer up under the field that fed it.
  return (
    <Panel title={section.title || "Result"}>
      <div className="field-grid">
        {section.outputs.map((o) => {
          const got = byKey.get(o.key);
          const stale = got && calc.stale;
          return (
            <div className="field" key={o.key}>
              <div className="field__top">
                <span className="field__label field__label--out">
                  {o.label}
                  {stale ? <span className="field__stale">out of date</span> : null}
                </span>
              </div>
              <div
                className={
                  stale ? "field__control field__control--stale" : "field__control"
                }
              >
                <output className="input input--result" htmlFor={o.key}>
                  {got ? formatValue(got) : ""}
                </output>
                {o.unit && !(got && isDate(got)) ? (
                  <span className="unit-static" title={o.unit}>
                    {o.unit}
                  </span>
                ) : null}
              </div>
            </div>
          );
        })}
      </div>
      {!calc.result && (
        <p className="result-hint">{remainingText(calc)}</p>
      )}
    </Panel>
  );
}

export function ScoreResultSection({
  section,
  calc,
}: {
  section: Extract<Section, { kind: "score_result" }>;
  calc: CalcState;
}) {
  const score = calc.result?.score;
  const total = score?.total ?? null;
  const tone = bandTone(score?.band?.label);

  return (
    <>
      <div
        className={[
          "hero",
          total === null ? "hero--pending" : "",
          total !== null && calc.stale ? "hero--stale" : "",
        ].filter(Boolean).join(" ")}
      >
        <div className="hero__label">
          {section.total?.label ?? "Total score"}
          {total !== null && calc.stale && <span className="hero__stale">out of date</span>}
        </div>
        <div className="score-total">
          <span className="score-total__value">
            {total === null ? "—" : formatNumber(total, 0)}
          </span>
          <span className="score-total__of">
            {section.total?.unit ?? "points"}
            {section.total?.max !== undefined && section.total?.max !== null
              ? ` of ${section.total.max}`
              : ""}
          </span>
        </div>
      </div>

      {score?.band && (
        <div className={`band band--${tone}`}>
          <span className="band__label">{score.band.label}</span>
          <span className="band__range">{bandRange(score.band)} points</span>
        </div>
      )}

      {section.bands.length > 0 && (
        <Panel title={section.title}>
        <div className="band-list">
          {section.bands.map((b, i) => {
            const active = score?.band?.label === b.label && bandRange(score.band) === bandRange(b);
            return (
              <div className={active ? "band-row band-row--active" : "band-row"} key={`${b.label}-${i}`}>
                <span className="band-row__range">{bandRange(b)}</span>
                <span>{b.label}</span>
              </div>
            );
          })}
        </div>
        </Panel>
      )}
    </>
  );
}

export function TitrationSection({
  section,
  calc,
}: {
  section: Extract<Section, { kind: "titration" }>;
  calc: CalcState;
}) {
  const table = calc.result?.table;
  if (!table || !table.rows) return null;
  const current = Number(calc.values[table.row_variable] ?? NaN);

  return (
    <Panel title={section.title} flush>
      <div className="table-wrap" style={{ maxHeight: 420 }}>
        <table className="data">
          <thead>
            <tr>
              <th className="num">
                {table.row_label}
                {table.row_unit ? ` (${table.row_unit})` : ""}
              </th>
              {table.columns.map((c: { key: string; label: string }) => (
                <th className="num" key={c.key}>
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((r: Record<string, number>, i: number) => (
              <tr key={i} className={Math.abs(r.dose - current) < 1e-9 ? "is-current" : undefined}>
                <td className="num">{formatNumber(r.dose)}</td>
                {table.columns.map((c: { key: string }) => (
                  <td className="num" key={c.key}>
                    {r[c.key] === null || r[c.key] === undefined ? "—" : formatNumber(r[c.key], 2)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {section.truncated_in_pdf && (
        <div style={{ padding: "12px 16px" }}>
          <div className="notice notice--warn">
            The printed dose ladder is cut off in the source document; rows beyond the
            last printed dose are not shown.
          </div>
        </div>
      )}
    </Panel>
  );
}

export function DrugTableSection({ calc }: { calc: CalcState }) {
  const drugs = calc.result?.drugs;
  if (!drugs) {
    return (
      <Panel title="Drugs">
        <p className="field__hint">Enter a weight to dose the table.</p>
      </Panel>
    );
  }

  return (
    <Panel title={`Drugs · ${drugs.weight ?? "—"} kg`} flush>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Drug</th>
              <th>Route</th>
              <th>Phase</th>
              <th className="num">Dose</th>
              <th className="num">Volume</th>
            </tr>
          </thead>
          <tbody>
            {drugs.drugs.flatMap((d) =>
              d.routes.flatMap((r, ri) =>
                r.doses.map((dose, i) => (
                  <tr
                    key={`${d.name}-${r.label}-${i}`}
                    className={ri === 0 && i === 0 ? "row-group" : "row-cont"}
                  >
                    <td>{i === 0 && ri === 0 ? d.name : ""}</td>
                    <td>{r.label}</td>
                    <td>{dose.phase ?? "—"}</td>
                    <td className="num">{quantity(dose.value, dose.unit)}</td>
                    <td className="num">{quantity(dose.volume_ml, "mL", 2)}</td>
                  </tr>
                )),
              ),
            )}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

export function ThresholdSection({
  section,
  calc,
}: {
  section: Extract<Section, { kind: "threshold_table" }>;
  calc: CalcState;
}) {
  const live = calc.result?.table?.kind === "thresholds" ? calc.result.table : null;

  return (
    <Panel title={section.title} flush>
      {live && (
        <div style={{ padding: 16, borderBottom: "1px solid var(--border)" }}>
          <div className="field__hint" style={{ marginBottom: 8 }}>
            At {formatNumber(live.age_hours)} hours,{" "}
            {live.risk_column === "anyRisk"
              ? `with ${live.risk_factors.length} neurotoxicity risk factor${live.risk_factors.length === 1 ? "" : "s"}`
              : "with no neurotoxicity risk factors"}
            {live.interpolated ? ` (interpolated between ${live.bracket[0]} and ${live.bracket[1]} hours)` : ""}
          </div>
          <div className="band-list">
            {Object.entries(live.thresholds as Record<string, number>).map(([k, v]) => (
              <div
                className={live.crossed.includes(k) ? "band-row band-row--active" : "band-row"}
                key={k}
              >
                <span className="band-row__range">{formatNumber(v, 1)} mg/dL</span>
                <span>{titleCase(k)}</span>
                {live.crossed.includes(k) && (
                  <span className="tag tag--accent" style={{ marginLeft: "auto" }}>
                    threshold reached
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="table-wrap" style={{ maxHeight: 340 }}>
        <table className="data">
          <thead>
            <tr>
              <th className="num">Age (h)</th>
              <th className="num">Photo — no risk</th>
              <th className="num">Exchange — no risk</th>
              <th className="num">Photo — any risk</th>
              <th className="num">Exchange — any risk</th>
            </tr>
          </thead>
          <tbody>
            {section.rows.map((r) => (
              <tr key={r.h}>
                <td className="num">{r.h}</td>
                <td className="num">{r.noRisk.photo}</td>
                <td className="num">{r.noRisk.exchange}</td>
                <td className="num">{r.anyRisk.photo}</td>
                <td className="num">{r.anyRisk.exchange}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

/**
 * The calculator's own reference data, as a table.
 *
 * A growth chart is 217 rows of LMS parameters, and the source document prints
 * them as a table. Flattened into prose they were an unreadable run of digits;
 * here the row the patient's own value lands on is marked, so the lookup the
 * calculator performed can be seen rather than taken on trust.
 */
export function LookupTableSection({
  section,
  calc,
}: {
  section: Extract<Section, { kind: "lookup_table" }>;
  calc: CalcState;
}) {
  const [openKey, setOpenKey] = useState<string | null>(null);
  // Which row was read is the server's answer, not a guess made here: these
  // ladders are keyed on a COMPUTED value -- age in months, not the age in
  // years that was typed -- so matching on the form's own fields would mark
  // the wrong row.
  const used = new Map((calc.result?.lookups ?? []).map((l) => [l.key, l]));

  return (
    <>
      {section.tables.map((t, i) => {
        const id = `${t.key}-${i}`;
        const open = openKey === id;
        const hit = used.get(t.key);
        const activeIndex =
          hit && (t.select_value == null || hit.select_value === t.select_value)
            ? hit.row_index
            : -1;

        return (
          <Panel
            key={id}
            title={
              section.tables.length > 1
                ? `${section.title} — ${t.variant_label ?? t.key}`
                : section.title
            }
            description={`${t.row_count} rows, indexed by ${titleCase(t.key)}`}
            flush
            action={
              <button
                className="btn btn--ghost btn--sm"
                onClick={() => setOpenKey(open ? null : id)}
              >
                {open ? "Show less" : `Show all ${t.row_count}`}
              </button>
            }
          >
            <div className="table-wrap" style={{ maxHeight: open ? 460 : 268 }}>
              <table className="data">
                <thead>
                  <tr>
                    <th className="num">{titleCase(t.key)}</th>
                    {t.columns.map((c) => (
                      <th className="num" key={c}>
                        {c}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {windowed(t.rows, activeIndex, open).map(([row, index]) => (
                    <tr key={index} className={index === activeIndex ? "is-current" : undefined}>
                      <td className="num">{formatNumber(row.threshold)}</td>
                      {t.columns.map((c) => (
                        <td className="num" key={c}>
                          {row[c] === undefined ? "—" : formatNumber(row[c], 6)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {activeIndex >= 0 && hit && (
              <div className="table-foot">
                {titleCase(t.key)} {formatNumber(hit.lookup_value)} → the highlighted row is
                the one this calculation read.
              </div>
            )}
          </Panel>
        );
      })}
    </>
  );
}

/** Collapsed, show the rows around the one in use; expanded, show them all. */
function windowed(
  rows: Record<string, number>[],
  active: number,
  open: boolean,
): [Record<string, number>, number][] {
  const all = rows.map((r, i) => [r, i] as [Record<string, number>, number]);
  if (open || rows.length <= 12) return all;
  // Centre the row in use rather than putting it at an edge: a lookup you can
  // only half see is not a lookup you can check.
  const centre = active >= 0 ? active : 0;
  const span = 11;
  const start = Math.max(0, Math.min(centre - Math.floor(span / 2), rows.length - span));
  return all.slice(start, start + span);
}

/** How many fields are still empty, said plainly. */
function remainingText(calc: CalcState): string {
  const missing = calc.schema.fields.filter(
    (f) => (calc.values[f.key] ?? "") === "" && !hidden(f, calc),
  ).length;
  if (missing === 0) return calc.submitted ? "calculating" : "press Calculate";
  return `${missing} field${missing === 1 ? "" : "s"} to go`;
}

/** Re-exported so the sections keep their single import. */
export function hidden(
  field: { visible_when?: { field: string; equals: (string | number | null)[] } | null },
  calc: CalcState,
): boolean {
  return isHidden(field, calc.values);
}

/**
 * A dose, whether it is one number or an ordered range.
 *
 * Infusions are prescribed as "0.1 to 0.5 mcg/kg/min", and the table states
 * them that way. Rendering only single numbers left every ranged dose as a
 * dash -- and those are the ones being titrated.
 */
function quantity(
  value: number | number[] | null,
  unit: string | null,
  decimals?: number,
): string {
  if (value === null || value === undefined) return "—";
  const suffix = unit ? ` ${unit}` : "";
  if (Array.isArray(value)) {
    if (!value.length) return "—";
    const [lo, hi] = [value[0], value[value.length - 1]];
    return lo === hi
      ? `${formatNumber(lo, decimals)}${suffix}`
      : `${formatNumber(lo, decimals)}–${formatNumber(hi, decimals)}${suffix}`;
  }
  return `${formatNumber(value, decimals)}${suffix}`;
}
