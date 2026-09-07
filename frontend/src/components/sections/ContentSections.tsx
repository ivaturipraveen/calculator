import type { Reference, Section } from "../../api/types";
import { Panel } from "../Panel";

export function FormulaSection({ section }: { section: Extract<Section, { kind: "formula" }> }) {
  return (
    <Panel title={section.title}>
      <div className="formula">
        {section.text.split("\n").map((line, i) => {
          const text = line.trim();
          if (!text) return null;
          if (isEquation(text)) {
            return (
              <div className="formula__eq" key={i}>
                {text}
              </div>
            );
          }
          if (text.endsWith(":") && text.length < 70) {
            return (
              <div className="formula__head" key={i}>
                {text}
              </div>
            );
          }
          return (
            <div className="formula__line" key={i}>
              {text}
            </div>
          );
        })}
      </div>
    </Panel>
  );
}

/**
 * Whether a line is the equation or the sentence around it.
 *
 * The source prints both together, and setting the whole block in monospace
 * makes the sentences hard to read while making the equation no easier. A line
 * with an "=" and arithmetic in it is the equation; the rest is prose.
 */
function isEquation(line: string): boolean {
  if (!line.includes("=")) return false;
  const rhs = line.slice(line.indexOf("=") + 1);
  return /[+\-*/^()]|\bx\b/.test(rhs) && /\d|[A-Za-z]/.test(rhs);
}

export function NotesSection({ section }: { section: Extract<Section, { kind: "notes" }> }) {
  const conditional = section.conditional_notes ?? [];
  return (
    <Panel title={section.title}>
      {section.instructions && (
        <div className="prose" style={{ marginBottom: 12 }}>
          <p>{section.instructions}</p>
        </div>
      )}
      {conditional.length > 0 && (
        <div className="notice notice--info" style={{ marginBottom: 12 }}>
          <ul>
            {conditional.map((n, i) => (
              <li key={i}>{typeof n === "string" ? n : (n?.text ?? JSON.stringify(n))}</li>
            ))}
          </ul>
        </div>
      )}
      <div className="prose">
        {section.notes.map((n, i) => (
          <p key={i}>{n}</p>
        ))}
      </div>
    </Panel>
  );
}

export function ReferencesSection({
  section,
}: {
  section: Extract<Section, { kind: "references" }>;
}) {
  // Open, like everything else the source document says. Folded behind a
  // summary these were citations nobody clicked, which for the evidence a dose
  // rests on is the same as not carrying them.
  return (
    <Panel title={`${section.title} (${section.items.length})`}>
      <ol className="ref-list">
        {section.items.map((r, i) => (
          <li key={i}>{renderReference(r)}</li>
        ))}
      </ol>
    </Panel>
  );
}

function renderReference(r: Reference) {
  const text = typeof r === "string" ? r : (r.text ?? "");
  const pubmed = typeof r === "object" ? (r.pubmed as string | undefined) : undefined;
  const url =
    (typeof r === "object" ? (r.url as string | undefined) : undefined) ??
    (pubmed ? `https://pubmed.ncbi.nlm.nih.gov/${pubmed}/` : undefined);
  return (
    <>
      {text}
      {url && (
        <>
          {" "}
          <a href={url} target="_blank" rel="noreferrer noopener">
            {pubmed ? `PubMed ${pubmed}` : "link"}
          </a>
        </>
      )}
    </>
  );
}

export function LegalSection({ section }: { section: Extract<Section, { kind: "legal" }> }) {
  // A disclaimer that has to be clicked open is a disclaimer that was not read.
  return (
    <Panel title={section.title}>
      <div className="prose prose--fine">
        {section.items.map((it) => (
          <p key={it.key}>{it.text}</p>
        ))}
      </div>
    </Panel>
  );
}

/**
 * A table the source prints beside the calculator.
 *
 * QT Interval Correction prints the normal range for each formula, by sex --
 * which is the thing that turns the number into a decision. Flattened into
 * lines it was unreadable; as a table it is the reference it was meant to be.
 */
export function ReferenceTableSection({
  section,
}: {
  section: Extract<Section, { kind: "reference_table" }>;
}) {
  const width = Math.max(...section.rows.map((r) => r.values.length), 0);
  return (
    <Panel title={section.title} flush>
      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>{section.column_header ?? ""}</th>
              {Array.from({ length: width }).map((_, i) => (
                <th className="num" key={i}>
                  {section.columns[i] ?? `Value ${i + 1}`}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {section.rows.map((r) => (
              <tr key={r.label}>
                <td>{r.label}</td>
                {Array.from({ length: width }).map((_, i) => (
                  <td className="num" key={i}>
                    {r.values[i] ?? "—"}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

/** Quantities the calculator holds constant, which its answer depends on. */
export function FixedValuesSection({
  section,
}: {
  section: Extract<Section, { kind: "fixed_values" }>;
}) {
  return (
    <Panel
      title={section.title}
      description="Stated on the source form, not asked for — the result assumes them."
    >
      <div className="value-list">
        {section.items.map((f) => (
          <div className="value-row" key={f.label}>
            <span className="value-row__label">{f.label}</span>
            <span className="value-row__value">
              {f.value}
              <span className="value-row__unit">{f.unit}</span>
            </span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

/**
 * Ranges the source enforces on values the calculator works out.
 *
 * Amiodarone's script refuses a daily dosage above 2.1 g and a concentration
 * outside 1-6 mg/mL. Neither is a field, so both were dropped -- and the
 * document's own safety limits vanished from the page that most needs them.
 */
export function StatedLimitsSection({
  section,
}: {
  section: Extract<Section, { kind: "stated_limits" }>;
}) {
  return (
    <Panel
      title={section.title}
      description="Checked by the source calculator on values it derives, not on what you enter."
    >
      <div className="value-list">
        {section.items.map((l) => (
          <div className="value-row" key={l.name}>
            <span className="value-row__label">{l.name}</span>
            <span className="value-row__value">
              {l.min !== undefined && l.max !== undefined
                ? `${l.min} – ${l.max}`
                : l.min !== undefined
                  ? `≥ ${l.min}`
                  : `≤ ${l.max}`}
              {l.unit ? <span className="value-row__unit"> {l.unit}</span> : null}
            </span>
          </div>
        ))}
      </div>
    </Panel>
  );
}
