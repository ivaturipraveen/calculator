import type { ReactNode } from "react";

/** A titled card. Every section on a calculator page is one of these. */
export function Panel({
  title,
  description,
  action,
  flush,
  children,
}: {
  title?: string;
  description?: string;
  action?: ReactNode;
  flush?: boolean;
  children: ReactNode;
}) {
  return (
    <section className="panel">
      {title && (
        <header className="panel__head">
          <div>
            <h2>{title}</h2>
            {description && <p>{description}</p>}
          </div>
          {action}
        </header>
      )}
      <div className={flush ? "panel__body panel__body--flush" : "panel__body"}>{children}</div>
    </section>
  );
}
