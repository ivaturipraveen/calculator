import { useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type { CalculatorSummary, CatalogMeta } from "../api/types";
import { hueStyle } from "../lib/hue";

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

/**
 * The landing page.
 *
 * Navigation is the index down the left -- one alphabetical list, always
 * there -- so this page no longer carries a second set of filters competing
 * with it. What it does instead is answer "what is in here": the specialties
 * and the shapes, as ways in, and the whole list when you want to browse it.
 */
export function CatalogPage({
  items,
  meta,
}: {
  items: CalculatorSummary[] | null;
  meta: CatalogMeta | null;
}) {
  const [params, setParams] = useSearchParams();
  const query = params.get("q") ?? "";
  const category = params.get("category");

  const shown = useMemo(() => {
    if (!items) return null;
    const q = query.trim().toLowerCase();
    return items
      .filter((c) => !category || c.category === category)
      .filter(
        (c) => !q || c.title.toLowerCase().includes(q) || (c.subtitle ?? "").toLowerCase().includes(q),
      )
      .sort((a, b) => a.title.localeCompare(b.title, undefined, { sensitivity: "base" }));
  }, [items, query, category]);

  const heading = query ? `Results for “${query}”` : (category ?? "All calculators");

  return (
    <main className="detail-pane">
      <div className="detail-inner detail-inner--wide">
        <div className="eyebrow-row">
          <span className="eyebrow">Index</span>
        </div>
        <h1>{heading}</h1>
        <p className="subtitle">
          {shown === null
            ? "Loading…"
            : `${shown.length} of ${items?.length ?? 0} clinical calculators, each extracted from its source document and checked against it field by field.`}
        </p>

        {(query || category) && (
          <div className="active-filters">
            {query && (
              <span className="filter-pill">
                “{query}”
                <button
                  onClick={() => {
                    const n = new URLSearchParams(params);
                    n.delete("q");
                    setParams(n, { replace: true });
                  }}
                  aria-label="Clear search"
                >
                  ×
                </button>
              </span>
            )}
            {category && (
              <span className="filter-pill">
                {category}
                <button
                  onClick={() => {
                    const n = new URLSearchParams(params);
                    n.delete("category");
                    setParams(n, { replace: true });
                  }}
                  aria-label="Clear specialty filter"
                >
                  ×
                </button>
              </span>
            )}
          </div>
        )}

        {!query && !category && meta && (
          <section className="browse">
            <h2 className="browse__title">By specialty</h2>
            <div className="browse__row">
              {meta.categories.map((c) => (
                <Link
                  key={c.name}
                  className="browse__chip"
                  style={hueStyle(c.name)}
                  to={`/?category=${encodeURIComponent(c.name)}`}
                >
                  <span className="browse__dot" />
                  {c.name}
                  <span className="browse__count">{c.count}</span>
                </Link>
              ))}
            </div>
          </section>
        )}

        {shown === null ? (
          <div className="card-grid">
            {Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="skeleton" style={{ height: 74 }} />
            ))}
          </div>
        ) : shown.length === 0 ? (
          <div className="empty">
            <p style={{ margin: 0, fontWeight: 600 }}>Nothing matches.</p>
            <p style={{ margin: "8px 0 14px" }}>
              Try a drug name, a score name, or clear the filters.
            </p>
            <button
              className="btn-reset"
              onClick={() => setParams(new URLSearchParams(), { replace: true })}
            >
              Show all calculators
            </button>
          </div>
        ) : (
          <div className="card-grid">
            {shown.map((c) => (
              <Link
                className="calc-card"
                key={c.slug}
                to={`/c/${encodeURIComponent(c.title)}`}
                style={hueStyle(c.category)}
              >
                <h3>{c.title}</h3>
                <div className="calc-card__meta">
                  <span className="tag">{RENDERER_LABEL[c.renderer] ?? c.renderer}</span>
                  {c.category && <span className="calc-card__cat">{c.category}</span>}
                  {c.limited && (
                    <span
                      className="calc-card__limited"
                      title="The source document did not carry everything this calculator needs"
                    >
                      limited
                    </span>
                  )}
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>
    </main>
  );
}
