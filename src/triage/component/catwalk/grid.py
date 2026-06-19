"""Leaf helpers extracted from ``model_trainers.py``.

These two pure-Python symbols are the only parts of the inherited ``ModelTrainer`` module that
non-training consumers (the CLI grid-size display, the individual-importance helper) still need.
Keeping them in a dependency-free leaf module lets those consumers stop importing
``model_trainers`` (which drags in the ORM-coupled training path), so the inherited trainer can
be deleted with the rest of the old flow.
"""

from sklearn.model_selection import ParameterGrid

NO_FEATURE_IMPORTANCE = (
    "Algorithm does not support a standard way to calculate feature importance."
)


def flatten_grid_config(grid_config):
    """Flatten a model/parameter grid configuration into individually trainable
    ``(class_path, parameters)`` pairs.

    Yields: (tuple) classpath and parameters
    """
    for class_path, parameter_config in grid_config.items():
        for parameters in ParameterGrid(parameter_config):
            yield class_path, parameters
