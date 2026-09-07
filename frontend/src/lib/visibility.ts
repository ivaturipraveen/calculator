/**
 * Whether the form is showing a field, given the choices made so far.
 *
 * `visible_when` records that the source form hides a field for some answers --
 * Valganciclovir's Schwartz coefficient applies to one sex, and showing both
 * invited a number the patient cannot have. It has to mean the same thing
 * everywhere: a field that is not on the form cannot be filled in, so counting
 * it as an outstanding requirement leaves the form waiting forever. That is
 * exactly what happened -- Valganciclovir sat on "calculating" and never
 * produced a dose, because it was waiting for the female coefficient while
 * showing the male one.
 */
export function isHidden(
  field: { visible_when?: { field: string; equals: (string | number | null)[] } | null },
  values: Record<string, string>,
): boolean {
  const rule = field.visible_when;
  if (!rule) return false;
  const chosen = values[rule.field];
  // The control that decides has not been answered yet: show the field rather
  // than guess which way it will go.
  if (chosen === undefined || chosen === "") return false;
  return !rule.equals.some((v) => String(v) === String(chosen));
}
