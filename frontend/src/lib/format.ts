import type { Band, OutputValue } from "../api/types";

/** Render a computed value the way the calculator itself states it. */
export function formatValue(v: OutputValue): string {
  if (typeof v.value === "string") return v.value;
  if (!Number.isFinite(v.value)) return "—";
  // An estimated date of confinement is a DATE. The formulas work in epoch
  // milliseconds, so it arrives as 1778198400000 -- which is the right number
  // and an unusable answer.
  if (v.kind === "date" || v.unit === "epoch_ms") return formatDate(v.value);
  const decimals = v.decimals ?? inferDecimals(v.value);
  return v.value.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

export function formatNumber(n: number, decimals?: number): string {
  if (!Number.isFinite(n)) return "—";
  const d = decimals ?? inferDecimals(n);
  return n.toLocaleString(undefined, {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
}

/** Enough places to be useful, not so many the number stops being readable. */
function inferDecimals(n: number): number {
  const abs = Math.abs(n);
  if (Number.isInteger(n)) return 0;
  if (abs >= 100) return 1;
  if (abs >= 1) return 2;
  return 3;
}

export function bandRange(b: Band): string {
  const { min, max } = b;
  if (min !== null && max !== null) return min === max ? `${min}` : `${min}–${max}`;
  if (min !== null) return `≥ ${min}`;
  if (max !== null) return `≤ ${max}`;
  return "";
}

/**
 * Colour a band by what it says, never by where it sits in the list.
 *
 * Band order is not severity order -- CHA2DS2-VASc counts up from low risk,
 * Apgar counts down from normal -- so a positional rule would paint half the
 * calculators backwards. Matching the words the source document uses is the
 * only signal that means the same thing everywhere, and anything unrecognised
 * stays neutral rather than guessing.
 */
export type Tone = "good" | "caution" | "bad" | "neutral";

const GOOD = /\b(low|normal|none|not required|no stunting|no wasting|mild|minimal|negative|unlikely|class a)\b/i;
const CAUTION = /\b(intermediate|moderate|borderline|possible|caution|elevated|overweight|class b)\b/i;
const BAD = /\b(high|severe|critical|very high|obese|profound|urgent|administer|likely|class c)\b/i;

export function bandTone(label: string | null | undefined): Tone {
  const text = label ?? "";
  if (BAD.test(text)) return "bad";
  if (CAUTION.test(text)) return "caution";
  if (GOOD.test(text)) return "good";
  return "neutral";
}

/** `MonthsOld` and `months_old` both read as "Months Old". */
export function titleCase(s: string): string {
  return s
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}


/**
 * Epoch milliseconds as the date they stand for.
 *
 * Read back in UTC, which is how they were built: the input side turns a date
 * picker's "2026-03-15" into midnight UTC, so a round trip has to land on the
 * same calendar day whatever the reader's timezone.
 */
export function formatDate(ms: number): string {
  if (!Number.isFinite(ms)) return "—";
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, {
    timeZone: "UTC",
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
