import type { Section } from "../../api/types";
import type { CalcState } from "../../hooks/useCalculator";
import { ConverterSection, FieldsSection, ScoreGroupsSection, TreeSection } from "./InputSections";
import {
  DrugTableSection,
  LookupTableSection,
  ResultsSection,
  ScoreResultSection,
  ThresholdSection,
  TitrationSection,
} from "./ResultSections";
import {
  FixedValuesSection,
  FormulaSection,
  LegalSection,
  NotesSection,
  ReferenceTableSection,
  ReferencesSection,
  StatedLimitsSection,
} from "./ContentSections";

/**
 * The one place a section `kind` is turned into a component.
 *
 * Every calculator page is this map applied to the server's plan. There is no
 * branch on a slug anywhere in the client, so a calculator whose shape differs
 * -- a new score, a new nomogram -- needs a kind added here and nothing else.
 */
export function SectionRenderer({ section, calc }: { section: Section; calc: CalcState }) {
  switch (section.kind) {
    case "fields":
      return <FieldsSection section={section} calc={calc} />;
    case "score_groups":
      return <ScoreGroupsSection section={section} calc={calc} />;
    case "converter":
      return <ConverterSection section={section} calc={calc} />;
    case "tree":
      return <TreeSection section={section} calc={calc} />;
    case "results":
      return <ResultsSection section={section} calc={calc} />;
    case "score_result":
      return <ScoreResultSection section={section} calc={calc} />;
    case "titration":
      return <TitrationSection section={section} calc={calc} />;
    case "drug_table":
      return <DrugTableSection calc={calc} />;
    case "threshold_table":
      return <ThresholdSection section={section} calc={calc} />;
    case "lookup_table":
      return <LookupTableSection section={section} calc={calc} />;
    case "reference_table":
      return <ReferenceTableSection section={section} />;
    case "stated_limits":
      return <StatedLimitsSection section={section} />;
    case "fixed_values":
      return <FixedValuesSection section={section} />;
    case "formula":
      return <FormulaSection section={section} />;
    case "notes":
      return <NotesSection section={section} />;
    case "references":
      return <ReferencesSection section={section} />;
    case "legal":
      return <LegalSection section={section} />;
    default:
      return null;
  }
}

/**
 * Which sections are the source document talking, rather than the clinician
 * working. These go in the reference column, open, all of them: a disclaimer
 * folded behind a summary is a disclaimer nobody read.
 */
/**
 * The answer, which the reference design lays out as boxes of the same shape as
 * the inputs -- so a form reads as one thing: what you enter, the button, what
 * comes back.
 */
export const RESULT_KINDS = new Set<Section["kind"]>([
  "results",
  "score_result",
]);

export const INFO_KINDS = new Set<Section["kind"]>([
  "formula",
  "notes",
  "references",
  "legal",
  "stated_limits",
  "fixed_values",
  "reference_table",
]);
