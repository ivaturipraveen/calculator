import { useCallback, useEffect, useState } from "react";
import { Link, Route, Routes } from "react-router-dom";
import { api } from "./api/client";
import { Sidebar } from "./components/Sidebar";
import type { CalculatorSummary, CatalogMeta } from "./api/types";
import { CatalogPage } from "./pages/CatalogPage";
import { CalculatorPage } from "./pages/CalculatorPage";
import "./styles/app.css";

type Theme = "system" | "light" | "dark";

/**
 * Reading a stored preference must never be able to stop the page rendering.
 * Storage is absent under a test renderer and throws outright in a browser set
 * to block site data, so a failure here falls back to following the system.
 */
function readPref(key: string, fallback: string): string {
  try {
    return window.localStorage?.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

function savePref(key: string, value: string): void {
  try {
    window.localStorage?.setItem(key, value);
  } catch {
    // A private window can refuse to store a preference; the page still works.
  }
}

/**
 * The shell: a fixed header, a permanent index down the left, and the page
 * itself. Nothing here scrolls -- each column scrolls on its own, so choosing
 * a different calculator never loses your place in the list.
 */
export default function App() {
  const [theme, setTheme] = useState<Theme>(
    () => readPref("calc-theme", "system") as Theme,
  );
  const [collapsed, setCollapsed] = useState(
    () => readPref("calc-index", "open") === "closed",
  );
  const [items, setItems] = useState<CalculatorSummary[] | null>(null);
  const [meta, setMeta] = useState<CatalogMeta | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    savePref("calc-theme", theme);
  }, [theme]);

  const collapse = useCallback((v: boolean) => {
    setCollapsed(v);
    savePref("calc-index", v ? "closed" : "open");
  }, []);

  // The index is loaded once, by the shell, rather than by each page: it is on
  // screen the whole time, so re-fetching it per route would be a flicker for
  // nothing.
  useEffect(() => {
    const c = new AbortController();
    Promise.all([api.list({}, c.signal), api.catalog(c.signal)])
      .then(([list, m]) => {
        setItems(list.calculators);
        setMeta(m);
      })
      .catch((e) => e.name !== "AbortError" && setFailed(String(e.message ?? e)));
    return () => c.abort();
  }, []);

  return (
    <div className={collapsed ? "shell shell--index-closed" : "shell"}>
      <Topbar theme={theme} onTheme={setTheme} count={items?.length ?? null} />

      {failed ? (
        <div className="app">
          <main className="detail-pane">
            <div className="detail-inner">
              <div className="notice notice--error" role="alert">
                <div>
                  <strong>Cannot reach the API.</strong>
                  <div style={{ marginTop: 4 }}>{failed}</div>
                  <div style={{ marginTop: 8 }}>
                    Start both with <code>python3 run.py</code> from the project root.
                  </div>
                </div>
              </div>
            </div>
          </main>
        </div>
      ) : (
        <div className="app">
          <Sidebar items={items} meta={meta} collapsed={collapsed} onCollapse={collapse} />
          <Routes>
            <Route path="/" element={<CatalogPage items={items} meta={meta} />} />
            <Route path="/c/:name" element={<CalculatorPage />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </div>
      )}
    </div>
  );
}

function Topbar({
  theme,
  onTheme,
  count,
}: {
  theme: Theme;
  onTheme: (t: Theme) => void;
  count: number | null;
}) {
  // No search box here: the index down the left filters as you type, is always
  // on screen, and is faster than a box that navigates away to a list.
  const cycle = () => onTheme(theme === "system" ? "light" : theme === "light" ? "dark" : "system");

  return (
    <header className="topbar">
      <Link className="brand" to="/">
        <span className="brand-icon" aria-hidden="true">
          Rx
        </span>
        <span>
          <b>Calculators</b>
          <small>Clinical reference</small>
        </span>
      </Link>

      <div className="spacer" />

      <div className="status">
        <span className="dot" aria-hidden="true" />
        {count === null ? "loading" : `${count} calculators`}
      </div>

      <button
        className="btn-icon"
        onClick={cycle}
        title={`Theme: ${theme}`}
        aria-label={`Theme: ${theme}. Click to change.`}
      >
        {theme === "dark" ? "☾" : theme === "light" ? "☀" : "◐"}
      </button>
    </header>
  );
}

function NotFound() {
  return (
    <main className="detail-pane">
      <div className="detail-inner">
        <h1>Page not found.</h1>
        <p>
          <Link to="/">Back to all calculators</Link>
        </p>
      </div>
    </main>
  );
}
