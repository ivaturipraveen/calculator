/**
 * A stable colour per specialty.
 *
 * The catalogue is 178 rows of grey text; without some variety it is a wall.
 * Hashing the specialty name onto eight fixed hues gives each one a colour it
 * keeps everywhere it appears — rail, card edge, meta dot — so the eye can
 * learn "Anthropometrics is the sandy one" instead of reading every label.
 * Hashing rather than assigning means a new specialty needs no code change.
 */
const HUES = 8;

export function hueVar(name: string | null | undefined): string {
  if (!name) return "var(--accent)";
  let h = 0;
  for (let i = 0; i < name.length; i++) {
    h = (h * 31 + name.charCodeAt(i)) >>> 0;
  }
  return `var(--hue-${h % HUES})`;
}

/** Inline style that sets `--dot` for a card or chip. */
export function hueStyle(name: string | null | undefined): React.CSSProperties {
  return { ["--dot" as string]: hueVar(name) } as React.CSSProperties;
}
