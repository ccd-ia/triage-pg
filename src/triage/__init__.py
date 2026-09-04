# -*- coding: utf-8 -*-

# Authorship of *this* package. The upstream project it derives from — DSSG
# triage, originated and led by Rayid Ghani at the Center for Data Science and
# Public Policy — is credited in CITATION.cff's `references` and in
# .zenodo.json's `contributors`, which is what the DOI record carries.
__author__ = """Adolfo De Unánue"""
__email__ = "adolfo+git@unanue.mx"
__version__ = "1.1.5"

from .logging import configure_logging

# Configure logging on import with default settings
configure_logging()
