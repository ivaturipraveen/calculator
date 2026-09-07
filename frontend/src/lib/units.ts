import type { FieldSchema, UnitOption } from "../api/types";

/**
 * A field's limits, expressed in the unit the clinician is typing in.
 *
 * The spec states every bound in the field's base unit, and the server converts
 * before it compares -- so a weight typed in pounds is checked against a limit
 * in kilograms only after it has become kilograms. The FORM was showing those
 * same base-unit numbers beside a box set to a different unit: "0-50 degC" next
 * to a box reading Degrees F, and `max=50` on the input itself. A normal body
 * temperature in Fahrenheit was rejected by the browser before the server ever
 * saw it, and an elevation in feet was capped at the metre limit.
 *
 * Units here are affine -- `base = raw * factor + offset` -- so the inverse is
 * `raw = (base - offset) / factor`. A negative factor would swap the ends; none
 * of the corpus has one, and the swap costs a comparison.
 */
export function unitByCode(field: FieldSchema, code: string | null): UnitOption | null {
  if (!code) return null;
  const want = squash(code);
  return field.unit.options.find((u) => squash(u.code) === want) ?? null;
}

export function fromBase(value: number, unit: UnitOption | null): number {
  if (!unit) return value;
  const factor = unit.factor ?? 1;
  const offset = unit.offset ?? 0;
  if (!factor) return value;
  return (value - offset) / factor;
}

/** Trim the noise an affine conversion leaves behind (98.60000000000001). */
export function tidy(n: number): number {
  if (!Number.isFinite(n)) return n;
  const r = Number(n.toPrecision(12));
  return Object.is(r, -0) ? 0 : r;
}

export interface DisplayBounds {
  min: number | null;
  max: number | null;
  unit: string | null;
  converted: boolean;
  openMin: boolean;
  openMax: boolean;
}

export function boundsIn(field: FieldSchema, code: string | null): DisplayBounds {
  const { min, max } = field.constraints;
  const base = field.unit.base;
  const shown = code ?? field.unit.display ?? base;
  const unit = unitByCode(field, shown);
  const isBase = !unit || !base || squash(shown ?? "") === squash(base);

  const openMin = !!field.constraints.exclusive_min;
  const openMax = !!field.constraints.exclusive_max;
  if (isBase || (min === null && max === null)) {
    return { min, max, unit: shown ?? null, converted: false, openMin, openMax };
  }
  const a = min === null ? null : tidy(fromBase(min, unit));
  const b = max === null ? null : tidy(fromBase(max, unit));
  const flip = a !== null && b !== null && a > b;
  return {
    min: flip ? b : a,
    max: flip ? a : b,
    unit: shown ?? null,
    converted: true,
    openMin: flip ? openMax : openMin,
    openMax: flip ? openMin : openMax,
  };
}

function squash(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]/g, "");
}
