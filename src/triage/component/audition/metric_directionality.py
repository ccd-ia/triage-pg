from triage.logging import get_logger

logger = get_logger(__name__)
import operator

# Directionality of every metric audition may rank by, inlined so audition does
# not import the (deletable) catwalk evaluation module. ``True`` means higher is
# better. Two name families coexist here:
#
#   * Greenfield (in-PG) metric names — produced by
#     ``triage.component.results_schema/alembic/versions/0002_metric_functions.py``
#     into ``triage.evaluations``: precision@/recall@/auc_roc/average_precision
#     (higher better); rmse/mae (lower better); r2 (higher better).
#   * Legacy (catwalk ModelEvaluator) metric names — kept so older configs and
#     fixtures still resolve: precision@/recall@/fbeta@/f1/accuracy/roc_auc/
#     average precision/true positives/true negatives (higher better);
#     false positives/false negatives/fpr (lower better).
GREATER_IS_BETTER = {
    # greenfield (triage.evaluations)
    "precision@": True,
    "recall@": True,
    "auc_roc": True,
    "average_precision": True,
    "rmse": False,
    "mae": False,
    "r2": True,
    # legacy (catwalk ModelEvaluator.available_metrics)
    "fbeta@": True,
    "f1": True,
    "accuracy": True,
    "roc_auc": True,
    "average precision score": True,
    "true positives@": True,
    "true negatives@": True,
    "false positives@": False,
    "false negatives@": False,
    "fpr@": False,
}


def greater_is_better(metric):
    """Whether or not a metric wants higher values

    Args:
        metric (str): The name of a metric, ie 'precision@'
    Returns: (bool) Whether or not greater is better for the metric
    """
    if metric in GREATER_IS_BETTER:
        return GREATER_IS_BETTER[metric]
    else:
        logger.warning(
            "Metric %s not found in available metrics, assuming greater is better",
            metric,
        )
        return True


def sql_rank_order(metric):
    """SQL Rank Order for a metric

    Args:
        metric (str): The name of a metric, ie 'precision@'
    Returns: (str) A SQL ORDER BY clause that will rank the best values first
    """
    if greater_is_better(metric):
        return "desc"
    else:
        return "asc"


def is_better_operator(metric):
    """Operator to decide which of two values is better

    Args:
        metric (str): The name of a metric, ie 'precision@'
    Returns: (function) An operator function that will compare two values
        and return whether or not the first one is better
    """
    if greater_is_better(metric):
        return operator.ge
    else:
        return operator.le


def best_in_series(metric):
    """The best value in a series

    Args:
        metric (str): The name of a metric, ie 'precision@'
    Returns: (str) The name of a pandas Series function that will provide
        the best value
    """
    if greater_is_better(metric):
        return "max"
    else:
        return "min"


def worst_in_series(metric):
    """The worst value in a series
    Args:
        metric (str): The name of a metric, ie 'precision@'
    Returns: (str) The name of a pandas Series function that will provide
        the worst value
    """
    if greater_is_better(metric):
        return "min"
    else:
        return "max"


def idxbest(metric):
    """Index of first occurrence of the best value

    Args:
        metric (str): The name of a metric, ie 'precision@'
    Returns: (str) The name of a pandas function that will provide
        the index of the first occurrence of the best value
    """
    if greater_is_better(metric):
        return "idxmax"
    else:
        return "idxmin"


def value_agg_funcs(metric, lang="sql"):
    """Aggregation functions for combining multiple metric values (e.g., from different random seeds):
        metric (str): The name of a metric, ie 'precision@'
        lang (str): Either 'sql' or 'pandas' to return appropriate function names
    Returns: (dict) Dictionary of function names to provide the desired
        aggregation of metric values
    """
    if lang == "sql":
        mean_fcn = "avg"
    elif lang == "pandas":
        mean_fcn = "mean"
    else:
        raise ValueError("lang must be sql or pandas")

    return {
        "worst": worst_in_series(metric),
        "best": best_in_series(metric),
        "mean": mean_fcn,
    }
