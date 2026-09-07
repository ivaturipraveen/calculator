/**
 * The API's contract, mirrored.
 *
 * A `Section` is the important type here: the server decides what this
 * calculator's page is made of, and the client has one component per `kind`.
 * Adding a calculator shape means adding a kind here and a component for it --
 * never a branch on a slug.
 */

export type RendererKind =
  | "formula"
  | "score"
  | "lms"
  | "convert"
  | "titration_table"
  | "dose_table"
  | "mmed"
  | "tree";

export interface CalculatorSummary {
  slug: string;
  title: string;
  subtitle: string | null;
  category: string | null;
  renderer: RendererKind;
  status: string;
  inputs: number;
  outputs: number;
  completeness: number;
  /** Set when the source document left this calculator unable to be trusted. */
  limited: string | null;
}

export interface UnitOption {
  code: string;
  label: string;
  factor?: number;
  offset?: number;
  is_base?: boolean;
}

export interface FieldOption {
  value: number | string | null;
  label: string | null;
  key?: string | null;
  variant_label?: string | null;
  variant_value?: number | string | null;
}

export interface FieldSchema {
  key: string;
  label: string;
  widget: "number" | "quantity" | "select" | "date";
  required: boolean;
  required_when: { field: string; equals: (string | number | null)[] } | null;
  default: number | string | null;
  help: string[] | null;
  dimension: string | null;
  constraints: {
    min: number | null;
    max: number | null;
    exclusive_min?: boolean;
    exclusive_max?: boolean;
    step: number;
  };
  constraints_by_mode:
    | { subject: string; unit: string | null; min?: number; max?: number }[]
    | null;
  unit: {
    base: string | null;
    display: string | null;
    options: UnitOption[];
    selectable: boolean;
  };
  options: FieldOption[];
  options_incomplete: boolean;
  depends_on: string | null;
  visible_when: { field: string; equals: (string | number | null)[] } | null;
  source: string | null;
}

export interface ScoreOption {
  key: string;
  label: string;
  points: number;
}

export interface ScoreGroup {
  key: string;
  label: string;
  widget: "radio_group" | "checkbox_group";
  selection: "single" | "multiple";
  required: boolean;
  options: ScoreOption[];
}

export interface Band {
  min: number | null;
  max: number | null;
  label: string;
  raw?: string | null;
}

export interface OutputSchema {
  key: string;
  label: string;
  unit: string | null;
  decimals: number | null;
  kind: string;
}

export type Section =
  | {
      id: string;
      kind: "fields";
      title: string;
      fields: FieldSchema[];
      headings?: string[] | null;
    }
  | { id: string; kind: "score_groups"; title: string; groups: ScoreGroup[]; total: any }
  | { id: string; kind: "score_result"; title: string; bands: Band[]; total: any }
  | { id: string; kind: "results"; title: string; outputs: OutputSchema[]; primary: string | null }
  | { id: string; kind: "converter"; title: string; pairs: ConversionPair[] }
  | { id: string; kind: "tree"; title: string; nodes: Record<string, TreeNode>; outcomes: Record<string, TreeOutcome> }
  | { id: string; kind: "titration"; title: string; truncated_in_pdf: boolean }
  | { id: string; kind: "drug_table"; title: string; count: number }
  | { id: string; kind: "threshold_table"; title: string; rows: ThresholdRow[] }
  | { id: string; kind: "lookup_table"; title: string; tables: LookupTable[] }
  | {
      id: string;
      kind: "reference_table";
      title: string;
      columns: string[];
      column_header: string | null;
      rows: { label: string; values: number[] }[];
    }
  | {
      id: string;
      kind: "stated_limits";
      title: string;
      items: {
        name: string;
        applies_to: string | null;
        min?: number;
        max?: number;
        unit?: string | null;
      }[];
    }
  | {
      id: string;
      kind: "fixed_values";
      title: string;
      items: { label: string; value: number; unit: string }[];
    }
  | { id: string; kind: "formula"; title: string; text: string }
  | { id: string; kind: "notes"; title: string; notes: string[]; conditional_notes: any[]; instructions: string | null }
  | { id: string; kind: "references"; title: string; items: Reference[] }
  | { id: string; kind: "legal"; title: string; items: { key: string; text: string }[] };

export interface ConversionPair {
  from: string;
  to: string;
  factor: number;
  offset?: number;
  source_slug?: string;
}

export interface TreeNode {
  q?: string;
  yes?: string;
  no?: string;
  reconstructed?: boolean;
}

export interface TreeOutcome {
  text: string;
  detail?: string;
  tone?: "good" | "bad" | "caution";
}

export interface LookupTable {
  key: string;
  columns: string[];
  operator: string | null;
  row_count: number;
  select_key: string | null;
  select_value: number | string | null;
  variant_label: string | null;
  rows: Record<string, number>[];
}

export interface ThresholdRow {
  h: number;
  noRisk: Record<string, number>;
  anyRisk: Record<string, number>;
}

export interface Reference {
  text?: string;
  pubmed?: string;
  url?: string;
  [k: string]: unknown;
}

export interface CalculatorSchema {
  slug: string;
  title: string;
  subtitle: string | null;
  category: string | null;
  renderer: RendererKind;
  status: string;
  version: string | null;
  display: { decimal_precision?: number | null };
  fields: FieldSchema[];
  outputs: OutputSchema[];
  scoring: {
    groups: ScoreGroup[];
    bands: Band[];
    total: { key: string; label: string; unit: string; min?: number; max?: number } | null;
    interpretation: { key: string; label: string; printed_rows?: string[] } | null;
  } | null;
  caveats: {
    kind: string;
    severity: "critical" | "high";
    detail: string;
    field: string | null;
  }[];
  sections: Section[];
  help: { general?: string[] | null; fields_with_help?: number };
  completeness: { score?: number; missing?: string[] };
  /** The PDF these numbers were extracted from. */
  source: string | null;
}

export interface CalculateRequest {
  inputs?: Record<string, unknown>;
  units?: Record<string, string>;
  selections?: Record<string, string | string[] | null>;
  answers?: string[];
  pair_index?: number | null;
  value?: number | null;
  reverse?: boolean;
}

export interface OutputValue {
  key: string;
  label: string;
  value: number | string;
  unit: string | null;
  decimals: number | null;
  kind: string;
  finite: boolean;
}

export interface CalculateResponse {
  slug: string;
  renderer: RendererKind;
  ok: boolean;
  errors: { field: string; message: string }[];
  warnings: string[];
  outputs: OutputValue[];
  lookups: {
    key: string;
    row_index: number;
    lookup_value: number;
    row: Record<string, number>;
    select_value: number | string | null;
  }[];
  score: {
    total: number;
    min: number | null;
    max: number | null;
    unit: string;
    label: string;
    band: Band | null;
    interpretation_label: string | null;
    selected: { group: string; option: string; label: string; points: number }[];
  } | null;
  table: any | null;
  drugs: { weight: number | null; drugs: DrugEntry[] } | null;
  conversion: {
    pair_index: number;
    from: string;
    to: string;
    value: number;
    result: number;
    reversed: boolean;
  } | null;
  tree: {
    node: string;
    question: string | null;
    outcome: TreeOutcome | null;
    trail: { node: string; question: string; answer: string }[];
    done: boolean;
    reconstructed: boolean;
  } | null;
}

export interface DrugEntry {
  name: string;
  routes: {
    label: string;
    concentration: number | null;
    concentration_unit: string | null;
    doses: {
      phase: string | null;
      per_kg: boolean;
      amount: number | null;
      unit: string | null;
      value: number | number[] | null;
      volume_ml: number | number[] | null;
      max: number | null;
      note: string | null;
    }[];
  }[];
}

export interface CatalogMeta {
  count: number;
  built: string | null;
  categories: { name: string; count: number }[];
  renderers: { name: string; count: number }[];
}
