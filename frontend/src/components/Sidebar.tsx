import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { Chevron } from "./Chevron";
import type { CalculatorSummary, CatalogMeta } from "../api/types";

/**
 * The calculator index: one alphabetical list, always on screen.
 *
 * Filing 178 calculators under a type and a specialty asked the reader to know
 * which drawer a thing was in before they could find it -- and "Formula" and
 * "Renal function" are both true of a creatinine clearance. An A-Z list asks
 * nothing: you know the name, or you type part of it. Category stays, as a
 * filter over the same list rather than as a second way of navigating.
 */
export function Sidebar({
  items,
  meta,
  collapsed,
  onCollapse,
}: {
  items: CalculatorSummary[] | null;
  meta: CatalogMeta | null;
  collapsed: boolean;
  onCollapse: (v: boolean) => void;
}) {
  const [filter, setFilter] = useState("");
  const [category, setCategory] = useState("");
  const location = useLocation();
  const listRef = useRef<HTMLDivElement>(null);

  const active = useMemo(() => {
    const m = /^\/c\/(.+)$/.exec(location.pathname);
    return m ? decodeURIComponent(m[1]).toLowerCase() : null;
  }, [location.pathname]);

  const shown = useMemo(() => {
    if (!items) return null;
    const q = filter.trim().toLowerCase();
    return items
      .filter((c) => !category || c.category === category)
      .filter((c) => !q || c.title.toLowerCase().includes(q) || c.slug.includes(q))
      .sort((a, b) => a.title.localeCompare(b.title, undefined, { sensitivity: "base" }));
  }, [items, filter, category]);

  // One heading per initial, so a long list still says where you are.
  const groups = useMemo(() => {
    if (!shown) return null;
    const out: { letter: string; rows: CalculatorSummary[] }[] = [];
    for (const c of shown) {
      const first = c.title.trim()[0]?.toUpperCase() ?? "#";
      const letter = /[A-Z]/.test(first) ? first : "#";
      if (out[out.length - 1]?.letter !== letter) out.push({ letter, rows: [] });
      out[out.length - 1].rows.push(c);
    }
    return out;
  }, [shown]);

  // Follow the selection when it changes from outside the list -- a search, a
  // link, the back button -- so the sidebar is never showing somewhere else.
  useEffect(() => {
    if (!active || collapsed) return;
    const el = listRef.current?.querySelector<HTMLElement>(".calc-item.is-active");
    // Scrolling the list is a convenience, so it must never be load-bearing:
    // where scrollIntoView is missing the throw escaped the effect and took the
    // whole sidebar down with it -- no index at all, rather than an index that
    // did not scroll.
    if (typeof el?.scrollIntoView === "function") el.scrollIntoView({ block: "nearest" });
  }, [active, collapsed, groups]);

  if (collapsed) {
    return (
      <div className="sidebar-reopen">
        <button
          className="btn-icon btn-icon--toggle"
          onClick={() => onCollapse(false)}
          aria-label="Show the calculator index"
          title="Show the calculator index"
        >
          <Chevron dir="right" />
        </button>
      </div>
    );
  }

  return (
    <nav className="index-pane" aria-label="All calculators">
      <div className="index-head">
        <div className="index-head-row">
          <h2>Calculators</h2>
          <button
            className="btn-icon btn-icon--toggle"
            onClick={() => onCollapse(true)}
            aria-label="Hide the calculator index"
            title="Hide the calculator index"
          >
            <Chevron dir="left" />
          </button>
        </div>

        <div className="search-wrap">
          <span className="icon" aria-hidden="true">
            ⌕
          </span>
          <input
            type="search"
            value={filter}
            placeholder="Filter calculators…"
            aria-label="Filter calculators by name"
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>

        <div className="category-filter">
          <select
            value={category}
            aria-label="Filter by specialty"
            onChange={(e) => setCategory(e.target.value)}
          >
            <option value="">All categories</option>
            {(meta?.categories ?? []).map((c) => (
              <option key={c.name} value={c.name}>
                {c.name} ({c.count})
              </option>
            ))}
          </select>
        </div>

        <div className="count-line">
          <span>{items ? `${items.length} total` : "loading…"}</span>
          <span>{shown ? `${shown.length} shown` : ""}</span>
        </div>
      </div>

      <div className="calc-list" ref={listRef}>
        {groups === null ? (
          <div className="calc-list__empty">Loading…</div>
        ) : groups.length === 0 ? (
          <div className="calc-list__empty">
            Nothing matches.
            <button
              className="btn-reset"
              onClick={() => {
                setFilter("");
                setCategory("");
              }}
            >
              Clear filters
            </button>
          </div>
        ) : (
          groups.map((g) => (
            <div key={g.letter}>
              <div className="group-label">{g.letter}</div>
              {g.rows.map((c) => (
                <Link
                  key={c.slug}
                  to={`/c/${encodeURIComponent(c.title)}`}
                  className={
                    active === c.title.toLowerCase() || active === c.slug
                      ? "calc-item is-active"
                      : "calc-item"
                  }
                >
                  <span className="calc-item__name">{c.title}</span>
                  <span className="chev" aria-hidden="true">
                    ›
                  </span>
                </Link>
              ))}
            </div>
          ))
        )}
      </div>
    </nav>
  );
}
