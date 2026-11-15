/*
 * The problem-type sets the dashboard's panels branch on — the one copy the model card,
 * its crosstabs panel and the group sheet read. They mirror src/triage/problem_types.py.
 */
import type { ProblemType } from './types'

/** The types whose rows split at a top-k cut (problem_types.CUT_CLASS_TYPES): a binary
 *  outcome, or a regression_ranking target read as positive when outcome > 0. The Rayid
 *  curve, the near-miss bands and the prototypes read only these; a plain regression
 *  model's labeled rows are all positive, so its precision would read a flat 1. */
export const CUT_TYPES: readonly ProblemType[] = ['classification', 'regression_ranking']

/** The types with a continuous forecast (problem_types.REGRESSION_TYPES). */
export const REGRESSION_TYPES: readonly ProblemType[] = ['regression', 'regression_ranking']
