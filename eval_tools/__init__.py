"""eval_tools — BUFS RAG chatbot evaluation utilities.

Houses both the legacy one-off analysis scripts (``_*.py``) and the new
``eval_tools.kpi`` package (the KPI-gated eval tool). Making this a package
lets ``eval_tools.kpi.scorer`` resolve from the repo root with no
``pyproject.toml`` change; the legacy ``_*.py`` scripts remain runnable as
plain ``python eval_tools/_<name>.py`` entrypoints.
"""
