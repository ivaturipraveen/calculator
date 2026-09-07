import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { isHidden } from "../lib/visibility";
import type { CalculateRequest, CalculateResponse, CalculatorSchema } from "../api/types";

export interface CalcState {
  schema: CalculatorSchema;
  values: Record<string, string>;
  units: Record<string, string>;
  selections: Record<string, string | string[]>;
  answers: string[];
  pairIndex: number;
  pairValue: string;
  pairReversed: boolean;
  result: CalculateResponse | null;
  pending: boolean;
  /** Whether the clinician has asked for an answer yet. */
  submitted: boolean;
  /** True once every value the chosen path needs is present. */
  complete: boolean;
  calculate: () => void;
  errorsByField: Record<string, string>;
  setValue: (key: string, value: string) => void;
  setUnit: (key: string, code: string) => void;
  setSelection: (group: string, option: string, multiple: boolean) => void;
  answer: (choice: "yes" | "no") => void;
  resetAnswers: () => void;
  undoAnswer: () => void;
  setPair: (index: number) => void;
  setPairValue: (v: string) => void;
  togglePairDirection: () => void;
  reset: () => void;
}

/**
 * Drive one calculator: hold the form, ask the server for the answer.
 *
 * Evaluation is the server's job, not the client's. The formulas, the lookup
 * ladders and the unit conversions all live in the spec, and a second
 * implementation in TypeScript would be a second set of rounding rules and a
 * second chance to be wrong. The cost is a request per edit, which is why the
 * call is debounced and every superseded request is aborted -- so a fast typist
 * cannot make an older answer arrive last and win.
 */
export function useCalculator(schema: CalculatorSchema): CalcState {
  const initialValues = useMemo(() => {
    const v: Record<string, string> = {};
    for (const f of schema.fields) {
      // A REQUIRED field starts empty, whatever the source document printed in
      // it. Those numbers are not clinical defaults -- they are whatever was in
      // the boxes when someone printed the page -- so A-a Gradient opened with
      // a temperature of 37, a pressure of 21 and a respiratory quotient of
      // 0.8 already filled in, and a clinician could read a gradient computed
      // from three values they never entered. The document's value is offered
      // as a placeholder instead: visible, and not submitted.
      //
      // An OPTIONAL field keeps its default, because there it means something:
      // MMED's thirteen opioid boxes default to zero for "not on this one", and
      // Calvert opens on Cockcroft-Gault because that is the method it uses.
      const seed = f.required ? "" : f.default;
      v[f.key] = seed !== null && seed !== undefined ? String(seed) : "";
    }
    return v;
  }, [schema]);

  const initialUnits = useMemo(() => {
    const u: Record<string, string> = {};
    for (const f of schema.fields) {
      if (f.unit.display) u[f.key] = f.unit.display;
    }
    return u;
  }, [schema]);

  const [values, setValues] = useState<Record<string, string>>(initialValues);
  const [units, setUnits] = useState<Record<string, string>>(initialUnits);
  const [selections, setSelections] = useState<Record<string, string | string[]>>({});
  const [answers, setAnswers] = useState<string[]>([]);
  const [pairIndex, setPairIndex] = useState(0);
  const [pairValue, setPairValue] = useState("1");
  const [pairReversed, setPairReversed] = useState(false);
  const [result, setResult] = useState<CalculateResponse | null>(null);
  const [pending, setPending] = useState(false);
  // Every source document says "once all the inputs are entered, click the
  // Calculate button". Computing the moment the last box is filled skips the
  // step where a clinician looks over what they typed -- so the first answer is
  // asked for. After that the page stays live, because a titration table or a
  // growth centile that does not follow the value you just changed is worse
  // than useless.
  // A decision tree has no form to fill in -- answering its questions IS the
  // interaction -- so it must not sit waiting for a button before it will ask
  // the first one.
  const gated = schema.renderer !== "tree";
  const [submitted, setSubmitted] = useState(!gated);

  useEffect(() => {
    setValues(initialValues);
    setUnits(initialUnits);
    setSelections({});
    setAnswers([]);
    setPairIndex(0);
    setPairValue("1");
    setPairReversed(false);
    setResult(null);
    setSubmitted(!gated);
  }, [initialValues, initialUnits, gated]);

  const abortRef = useRef<AbortController | null>(null);

  // A field the clinician has not filled in yet is not an error -- it is an
  // unfinished form. Only what has been entered is sent, so the server reports
  // "required" once, on submit-shaped interactions, rather than on every
  // keystroke into the first box.
  const request = useMemo<CalculateRequest>(() => {
    const inputs: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(values)) {
      if (v !== "" && v !== null && v !== undefined) inputs[k] = v;
    }
    return {
      inputs,
      units,
      selections,
      answers,
      pair_index: schema.renderer === "convert" ? pairIndex : null,
      value: schema.renderer === "convert" ? Number(pairValue) || 0 : null,
      reverse: pairReversed,
    };
  }, [values, units, selections, answers, pairIndex, pairValue, pairReversed, schema.renderer]);

  const complete = useMemo(() => {
    if (schema.renderer === "convert") return pairValue.trim() !== "";
    if (schema.renderer === "tree") return true;
    if (schema.renderer === "score") {
      const groups = schema.scoring?.groups ?? [];
      return groups.some((g) => selections[g.key] !== undefined);
    }
    // A field the chosen path never reads is not part of "is this form
    // finished". Fentanyl asks for a bolus dose only when a bolus is given;
    // waiting for it left the infusion rate uncalculated over a box the answer
    // does not touch. Neither is a field the form is not even showing.
    const needed = (f: (typeof schema.fields)[number]) => {
      if (isHidden(f, values)) return false;
      if (f.required) return true;
      const rule = f.required_when;
      if (!rule) return false;
      const chosen = values[rule.field] ?? "";
      if (chosen === "") return false;
      return rule.equals.some((v) => String(v) === String(chosen));
    };
    const required = schema.fields.filter(needed);
    const pool = required.length ? required : schema.fields;
    if (!pool.length) return true;
    return pool.every((f) => (values[f.key] ?? "") !== "");
  }, [schema, values, selections, pairValue]);

  useEffect(() => {
    if (!complete) {
      setResult(null);
      return;
    }
    if (!submitted) return;
    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    setPending(true);

    const timer = window.setTimeout(() => {
      api
        .calculate(schema.slug, request, controller.signal)
        .then((r) => {
          if (!controller.signal.aborted) setResult(r);
        })
        .catch((e) => {
          if (e?.name !== "AbortError") {
            setResult({
              slug: schema.slug,
              renderer: schema.renderer,
              ok: false,
              errors: [{ field: "_", message: String(e?.message ?? e) }],
              warnings: [],
              outputs: [],
              lookups: [],
              score: null,
              table: null,
              drugs: null,
              conversion: null,
              tree: null,
            });
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) setPending(false);
        });
    }, 180);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [request, complete, submitted, schema.slug, schema.renderer]);

  const errorsByField = useMemo(() => {
    const out: Record<string, string> = {};
    for (const e of result?.errors ?? []) out[e.field] = e.message;
    return out;
  }, [result]);

  const setValue = useCallback((key: string, value: string) => {
    setValues((prev) => ({ ...prev, [key]: value }));
  }, []);

  const setUnit = useCallback((key: string, code: string) => {
    setUnits((prev) => ({ ...prev, [key]: code }));
  }, []);

  const setSelection = useCallback((group: string, option: string, multiple: boolean) => {
    setSelections((prev) => {
      if (!multiple) return { ...prev, [group]: option };
      const current = Array.isArray(prev[group]) ? (prev[group] as string[]) : [];
      return {
        ...prev,
        [group]: current.includes(option)
          ? current.filter((o) => o !== option)
          : [...current, option],
      };
    });
  }, []);

  const answer = useCallback((choice: "yes" | "no") => {
    setAnswers((prev) => [...prev, choice]);
  }, []);

  const calculate = useCallback(() => setSubmitted(true), []);

  const reset = useCallback(() => {
    setSubmitted(!gated);
    setValues(initialValues);
    setUnits(initialUnits);
    setSelections({});
    setAnswers([]);
    setPairValue("1");
    setPairReversed(false);
    setResult(null);
    setSubmitted(!gated);
  }, [initialValues, initialUnits, gated]);

  return {
    schema,
    submitted,
    complete,
    calculate,
    values,
    units,
    selections,
    answers,
    pairIndex,
    pairValue,
    pairReversed,
    result,
    pending,
    errorsByField,
    setValue,
    setUnit,
    setSelection,
    answer,
    resetAnswers: () => setAnswers([]),
    undoAnswer: () => setAnswers((p) => p.slice(0, -1)),
    setPair: setPairIndex,
    setPairValue,
    togglePairDirection: () => setPairReversed((p) => !p),
    reset,
  };
}
