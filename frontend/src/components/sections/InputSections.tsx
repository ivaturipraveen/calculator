import type { Section } from "../../api/types";
import type { CalcState } from "../../hooks/useCalculator";
import { Field } from "../Field";
import { Panel } from "../Panel";
import { hidden } from "./ResultSections";

export function FieldsSection({ section, calc }: { section: Extract<Section, { kind: "fields" }>; calc: CalcState }) {
  // A field the source form hides for the choices made so far is not a field
  // this form should ask for either -- Valganciclovir's k coefficient applies
  // to one sex, and showing both invited a number the patient cannot have.
  const shown = section.fields.filter((f) => !hidden(f, calc));
  const filled = shown.filter((f) => (calc.values[f.key] ?? "") !== "").length;
  const total = shown.length;

  return (
    <Panel
      title={section.title}
      action={
        <span className="tag" aria-label={`${filled} of ${total} fields entered`}>
          {filled}/{total}
        </span>
      }
    >
      {/* The PDF names the groups its form uses but not which fields sit in
          them, so they are named rather than guessed at. */}
      {section.headings?.length ? (
        <div className="form-groups">
          <span className="form-groups__intro">Groups on the source form</span>
          {section.headings.map((h) => (
            <span className="form-groups__chip" key={h}>
              {h}
            </span>
          ))}
        </div>
      ) : null}

      <div className="field-grid">
        {shown.map((f) => (
          <Field
            key={f.key}
            field={f}
            value={calc.values[f.key] ?? ""}
            unit={calc.units[f.key] ?? null}
            error={calc.errorsByField[f.key]}
            onValue={(v) => calc.setValue(f.key, v)}
            onUnit={(u) => calc.setUnit(f.key, u)}
          />
        ))}
      </div>
    </Panel>
  );
}

export function ScoreGroupsSection({
  section,
  calc,
}: {
  section: Extract<Section, { kind: "score_groups" }>;
  calc: CalcState;
}) {
  return (
    <Panel title={section.title} flush>
      <div className="criteria">
        {section.groups.map((g) => {
          const multiple = g.selection === "multiple";
          const picked = calc.selections[g.key];
          const chosen = Array.isArray(picked) ? picked : picked ? [picked] : [];
          const error = calc.errorsByField[g.key];
          return (
            // The styling lives in the stylesheet: inline rules here beat the
            // wide-screen two-column layout and the criteria stayed in one
            // long strip on a desktop.
            <fieldset className="criterion" key={g.key}>
              <legend className="criterion__label">
                {g.label}
                {multiple && <span className="criterion__any">choose any that apply</span>}
              </legend>
              {/* The border/background live here, not on the fieldset -- a
                  <legend> straddles its fieldset's own border by native
                  browser default, which on the bordered desktop card cut the
                  heading text in half across the top edge. */}
              <div className="criterion__body">
                <div className="choice-list">
                  {g.options.map((o) => (
                    <label className="choice" key={o.key}>
                      <input
                        type={multiple ? "checkbox" : "radio"}
                        name={g.key}
                        checked={chosen.includes(o.key)}
                        onChange={() => calc.setSelection(g.key, o.key, multiple)}
                      />
                      <span className="choice__text">{o.label}</span>
                      <span className="choice__points">
                        {o.points > 0 ? `+${o.points}` : o.points}
                      </span>
                    </label>
                  ))}
                </div>
                {error && (
                  <div className="field__error" role="alert" style={{ marginTop: 8 }}>
                    {error}
                  </div>
                )}
              </div>
            </fieldset>
          );
        })}
      </div>
    </Panel>
  );
}

export function ConverterSection({
  section,
  calc,
}: {
  section: Extract<Section, { kind: "converter" }>;
  calc: CalcState;
}) {
  const pair = section.pairs[calc.pairIndex];
  const from = calc.pairReversed ? pair?.to : pair?.from;
  const to = calc.pairReversed ? pair?.from : pair?.to;
  const conversion = calc.result?.conversion;

  return (
    <Panel title={section.title}>
      <div className="field-grid">
        <div className="field">
          <label className="field__label" htmlFor="conv-pair">
            Conversion
          </label>
          <select
            id="conv-pair"
            className="select"
            value={calc.pairIndex}
            onChange={(e) => calc.setPair(Number(e.target.value))}
          >
            {section.pairs.map((p, i) => (
              <option key={`${p.from}-${p.to}-${i}`} value={i}>
                {p.from} → {p.to}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label className="field__label" htmlFor="conv-value">
            Value
            <span className="field__hint">{from}</span>
          </label>
          <div className="field__control">
            <input
              id="conv-value"
              className="input"
              type="number"
              inputMode="decimal"
              value={calc.pairValue}
              onChange={(e) => calc.setPairValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "e" || e.key === "E") e.preventDefault();
              }}
            />
            <button
              type="button"
              className="btn btn--ghost"
              style={{ minWidth: 44, padding: "0 12px" }}
              onClick={calc.togglePairDirection}
              title={`Swap to ${to} → ${from}`}
              aria-label={`Swap direction, currently ${from} to ${to}`}
            >
              ⇄
            </button>
          </div>
        </div>
      </div>

      {conversion && (
        <div className="hero" style={{ marginTop: 16 }}>
          <div className="hero__label">
            {conversion.value} {conversion.from} equals
          </div>
          <div className="hero__value">
            {conversion.result.toLocaleString(undefined, { maximumFractionDigits: 6 })}
            <span className="hero__unit">{conversion.to}</span>
          </div>
        </div>
      )}
    </Panel>
  );
}

export function TreeSection({
  section,
  calc,
}: {
  section: Extract<Section, { kind: "tree" }>;
  calc: CalcState;
}) {
  const tree = calc.result?.tree;
  const outcome = tree?.outcome;
  const tone =
    outcome?.tone === "good" ? "band--good" : outcome?.tone === "bad" ? "band--bad" : "band--caution";

  return (
    <Panel
      title={section.title}
      flush
      action={
        calc.answers.length > 0 ? (
          <button className="btn btn--ghost" style={{ minHeight: 30, padding: "0 12px", fontSize: 12.5 }} onClick={calc.resetAnswers}>
            Start over
          </button>
        ) : null
      }
    >
      {outcome ? (
        <div className="tree-q">
          <div className={`band ${tone}`} style={{ textAlign: "left" }}>
            <div className="band__label">{outcome.text}</div>
            {outcome.detail && <div className="band__range">{outcome.detail}</div>}
          </div>
          <div className="tree-actions" style={{ marginTop: 16 }}>
            <button className="btn" onClick={calc.undoAnswer}>
              Back
            </button>
            <button className="btn btn--primary" onClick={calc.resetAnswers}>
              Start over
            </button>
          </div>
        </div>
      ) : (
        <div className="tree-q">
          <div className="tree-q__step">Question {calc.answers.length + 1}</div>
          <p className="tree-q__text">{tree?.question ?? "…"}</p>
          <div className="tree-actions">
            <button className="btn btn--primary" onClick={() => calc.answer("yes")}>
              Yes
            </button>
            <button className="btn" onClick={() => calc.answer("no")}>
              No
            </button>
            {calc.answers.length > 0 && (
              <button className="btn btn--ghost" onClick={calc.undoAnswer}>
                Back
              </button>
            )}
          </div>
        </div>
      )}

      {tree?.trail?.length ? (
        <div className="trail">
          {tree.trail.map((t, i) => (
            <div className="trail__row" key={`${t.node}-${i}`}>
              <span className="trail__answer">{t.answer}</span>
              <span>{t.question}</span>
            </div>
          ))}
        </div>
      ) : null}
    </Panel>
  );
}
