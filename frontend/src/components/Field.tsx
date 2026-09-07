import { useId, useState } from "react";
import type { FieldSchema } from "../api/types";
import { boundsIn } from "../lib/units";

interface Props {
  field: FieldSchema;
  value: string;
  unit: string | null;
  error?: string;
  onValue: (value: string) => void;
  onUnit: (code: string) => void;
}

/**
 * One input, built entirely from its schema.
 *
 * The three shapes the corpus actually has: a choice whose option carries a
 * coefficient, a number with a unit the clinician can switch, and a date. A
 * unit picker sits beside the number rather than under it, so a value and the
 * unit it is in are read as one thing -- which is how they are written on a
 * chart.
 *
 * Everything that is not the question and the box lives on the label's own
 * line: the range as a chip, the document's guidance behind one small button.
 * Spelled out under every field instead, the A-a Gradient form carried seven
 * grey "Guidance (2)" rows and the seven boxes were the smallest thing on it.
 */
export function Field({ field, value, unit, error, onValue, onUnit }: Props) {
  const id = `f-${field.key}`;
  const errId = `${id}-err`;
  const helpId = useId();
  const [showHelp, setShowHelp] = useState(false);
  const { step } = field.constraints;
  // The limits belong to the unit in the box beside them, not to the base unit
  // the spec states them in.
  const shownUnit = unit ?? field.unit.display ?? field.unit.base;
  const { min, max, unit: boundUnit, openMin, openMax } = boundsIn(field, shownUnit);
  const units = field.unit.options;
  const help = field.help ?? [];
  const describedBy = [error ? errId : null, showHelp ? helpId : null]
    .filter(Boolean)
    .join(" ");

  const range =
    !field.options.length && (min !== null || max !== null)
      ? min !== null && max !== null
        ? `${openMin ? "›" : ""}${min}–${openMax ? "‹" : ""}${max}`
        : min !== null
          ? `${openMin ? ">" : "≥"} ${min}`
          : `${openMax ? "<" : "≤"} ${max}`
      : null;

  return (
    <div className={error ? "field field--invalid" : "field"}>
      <div className="field__top">
        <label className="field__label" htmlFor={id}>
          {field.label}
          {field.required ? (
            <span className="field__req" aria-hidden="true">
              *
            </span>
          ) : field.required_when ? (
            <span
              className="field__when"
              title={`Needed when ${field.required_when.field.replace(/_/g, " ")} is ${field.required_when.equals.join(" or ")}`}
            >
              if {String(field.required_when.equals[0])}
            </span>
          ) : null}
        </label>

        <span className="field__aside">
          {range && (
            <span className="field__range" title={`Accepted range${boundUnit ? ` in ${boundUnit}` : ""}`}>
              {range}
              {boundUnit ? <span className="field__range-unit">{boundUnit}</span> : null}
            </span>
          )}
          {help.length > 0 && (
            <button
              type="button"
              className="field__info"
              aria-expanded={showHelp}
              aria-controls={helpId}
              aria-label={`Guidance for ${field.label}`}
              onClick={() => setShowHelp((v) => !v)}
            >
              i
            </button>
          )}
        </span>
      </div>

      <div className="field__control">
        {field.options.length > 0 ? (
          <select
            id={id}
            className="select"
            value={value}
            aria-invalid={error ? true : undefined}
            aria-describedby={describedBy || undefined}
            onChange={(e) => onValue(e.target.value)}
          >
            <option value="">Select…</option>
            {field.options.map((o, i) => (
              // Two age bands can legitimately carry the same coefficient
              // (valganciclovir's k is 0.45 for two of them), so the position
              // is part of the identity, not a fallback for a missing value.
              <option key={`${i}-${o.key ?? o.value ?? ""}`} value={String(o.value)}>
                {o.label ?? String(o.value)}
                {o.variant_label ? ` — ${o.variant_label}` : ""}
              </option>
            ))}
          </select>
        ) : (
          <>
            <input
              id={id}
              className="input"
              type={field.widget === "date" ? "date" : "number"}
              inputMode="decimal"
              value={value}
              step={field.widget === "date" ? undefined : step}
              min={field.widget === "date" || openMin ? undefined : (min ?? undefined)}
              max={field.widget === "date" || openMax ? undefined : (max ?? undefined)}
              placeholder={
                field.widget === "date"
                  ? undefined
                  : field.default !== null && field.default !== undefined
                    ? String(field.default)
                    : "—"
              }
              aria-invalid={error ? true : undefined}
              aria-describedby={describedBy || undefined}
              onChange={(e) => onValue(e.target.value)}
            />
            {field.unit.selectable && (
              <select
                className="select unit-select"
                value={unit ?? field.unit.display ?? ""}
                aria-label={`Unit for ${field.label}`}
                onChange={(e) => onUnit(e.target.value)}
              >
                {units.map((u) => (
                  <option key={u.code} value={u.code}>
                    {u.label}
                  </option>
                ))}
              </select>
            )}
            {!field.unit.selectable && field.unit.display && (
              <span className="unit-static" aria-hidden="true">
                {field.unit.display}
              </span>
            )}
          </>
        )}
      </div>

      {error && (
        <div className="field__error" id={errId} role="alert">
          {error}
        </div>
      )}

      {showHelp && help.length > 0 && (
        <ul className="field__help" id={helpId}>
          {help.map((h) => (
            <li key={h}>{h}</li>
          ))}
        </ul>
      )}

      {field.options_incomplete && (
        <div className="field__note">Only the options the source document prints are listed.</div>
      )}
    </div>
  );
}
