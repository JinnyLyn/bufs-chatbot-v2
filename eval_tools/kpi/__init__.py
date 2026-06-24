"""eval_tools.kpi — KPI-gated eval tool for the BUFS RAG chatbot.

Foundation spine (this commit): ``schema`` (canonical record contract +
4-shape field mapping) and ``scorer`` (the corrected canonical scoring
lineage). Both layers are PURE — no ``import config``, no file/network I/O —
so they run in the default offline ``pytest -m "not integration"`` lane.

Downstream modules (added by later workstreams): ``dataset``, ``profiles``,
``gate``, ``baseline``, ``report``, ``cli`` and the ``runners``/``probes``/
``sources`` subpackages.
"""
