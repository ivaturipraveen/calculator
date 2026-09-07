import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

const KEY = "calc-info-width";
const MIN = 300;
const MAX = 760;

function readWidth(): number {
  try {
    const n = Number(window.localStorage?.getItem(KEY));
    return Number.isFinite(n) && n >= MIN && n <= MAX ? n : 440;
  } catch {
    return 440;
  }
}

/**
 * The reference column: what the source document says, open.
 *
 * It is draggable because the two things it holds compete -- a wide formula
 * wants room, a page of notes wants a comfortable measure -- and which one
 * matters is the reader's call, not ours. The width is remembered, so it is a
 * decision made once.
 */
export function InfoPane({ title, children }: { title: string; children: ReactNode }) {
  const [width, setWidth] = useState(readWidth);
  const [dragging, setDragging] = useState(false);
  const [open, setOpen] = useState(true);
  const frame = useRef<number | null>(null);

  const onDown = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    setDragging(true);
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  }, []);

  useEffect(() => {
    if (!dragging) return;
    const move = (e: PointerEvent) => {
      if (frame.current !== null) return;
      frame.current = window.requestAnimationFrame(() => {
        frame.current = null;
        const next = Math.min(MAX, Math.max(MIN, window.innerWidth - e.clientX));
        setWidth(next);
      });
    };
    const up = () => {
      setDragging(false);
      try {
        window.localStorage?.setItem(KEY, String(Math.round(width)));
      } catch {
        // storage can be refused; the pane still works this session
      }
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      if (frame.current !== null) window.cancelAnimationFrame(frame.current);
      frame.current = null;
    };
  }, [dragging, width]);

  if (!open) {
    return (
      <div className="info-reopen">
        <button
          className="btn-icon btn-icon--toggle"
          onClick={() => setOpen(true)}
          aria-label="Show formula and references"
          title="Show formula and references"
        >
          ◂
        </button>
      </div>
    );
  }

  return (
    <>
      <div
        className={dragging ? "info-resizer dragging" : "info-resizer"}
        onPointerDown={onDown}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the reference column"
        title="Drag to resize"
      />
      <aside className="info-pane" style={{ width }} aria-label={title}>
        <div className="info-pane-head">
          <span>{title}</span>
          <button
            className="btn-icon btn-icon--bare btn-icon--toggle"
            onClick={() => setOpen(false)}
            aria-label="Hide formula and references"
            title="Hide"
          >
            ▸
          </button>
        </div>
        {children}
      </aside>
    </>
  );
}
