/*
 * The error-tree leaf kinds each problem type has — the one copy the model card's
 * ErrorRulesPanel and the group sheet's ErrorRecurrencePanel both read. It mirrors
 * problem_types.leaf_kinds (derived from error_tree_refusal), the gate the routes apply:
 *   - a top-k cut (classification, regression_ranking): fp, fn and confusion;
 *   - a survival model: the same three, fitted at a horizon (parameter `<cut>|horizon=<H>`);
 *   - a continuous forecast (regression, regression_ranking): abs_error and beats_baseline.
 */
import type { ErrorKind, ProblemType } from './types'

export interface KindTab {
  kind: ErrorKind
  label: string
  title: string
}

const CUT_KINDS: KindTab[] = [
  { kind: 'fp', label: 'FP · wrong flags', title: 'within the top-k: where the model over-selects' },
  { kind: 'fn', label: 'FN · missed need', title: 'among the passed-over: where the model misses positives' },
  {
    kind: 'confusion',
    label: 'Confusion · all four',
    title: 'every labeled row, TP/FP/FN/TN: the first splits mostly redraw the cut itself',
  },
]

const REGRESSION_KINDS: KindTab[] = [
  {
    kind: 'abs_error',
    label: '|error| · large misses',
    title: 'where the absolute error is large: each leaf’s mean |e| over the population MAE',
  },
  {
    kind: 'beats_baseline',
    label: 'vs baseline · not closer',
    title: 'where the model is not closer than its baseline, fitted on the earlier test dates',
  },
]

/** The kinds a model of each problem type has (problem_types.leaf_kinds). */
export const KINDS_OF: Record<ProblemType, KindTab[]> = {
  classification: CUT_KINDS,
  regression_ranking: [...CUT_KINDS, ...REGRESSION_KINDS],
  regression: REGRESSION_KINDS,
  survival: CUT_KINDS,
}

/** The kinds of a problem type; with no single type (a group whose models span several),
 *  every kind, and the route refuses a kind none of the group's types has. */
export function kindsOf(problemType: ProblemType | null | undefined): KindTab[] {
  return problemType ? KINDS_OF[problemType] : [...CUT_KINDS, ...REGRESSION_KINDS]
}
