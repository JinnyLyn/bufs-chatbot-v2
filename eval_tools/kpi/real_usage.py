"""``real_usage`` KPI family — the raison d'être: benchmark↔real accuracy gap.

combined88 is *clean and curated*; its ~85% contains **overstates** real-world
accuracy because real users type ambiguous / typo'd / mis-spaced queries and the
pipeline's gating components (``is_clear`` clarity gate, kiwi tokenizer,
``rewrite_query``, fast-refuse) misbehave on them. This family rolls the
Real-Usage Robustness Suite into one set of numbers, with a single **HEADLINE**:

    benchmark_real_gap_pp = clean_contains_rate − real_contains_rate   (in pp)

Small gap → the benchmark is trustworthy. Large gap → the benchmark *lies*, and
is itself a NO-GO. The gap floor (``max_gap_pp``) is **provisional → advisory
until measured** (same rule as the unmeasured h100-fast floors): this module
only *computes and reports*; ``gate.py`` owns the GO/NO-GO decision.

Pure + ``config``-free: consumes already-computed :class:`ScoreResult` triples
and probe outputs; no I/O, no network, no ``import config``. Probes that were
SKIPPED (live-only / MCP unreachable) arrive as ``None`` and are reported as
``None`` — never silently treated as a pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .probes.clarity import ClarityMetrics
from .probes.refuse import RefuseMetrics
from .probes.rewrite import RewriteDrift
from .probes.tokenizer import RecallDrop
from .scorer import ScoreResult

# Provisional headline floor: a clean→real contains drop larger than this flags
# the benchmark as untrustworthy. ADVISORY until measured from real data.
MAX_GAP_PP_ADVISORY = 10.0


def benchmark_real_gap_pp(clean_contains_rate: float, real_contains_rate: float) -> float:
    """HEADLINE KPI: clean-set contains − real-usage contains, in percentage points."""
    return (clean_contains_rate - real_contains_rate) * 100.0


@dataclass(frozen=True)
class RealUsageFamily:
    """The ``real_usage`` KPI family + the headline benchmark↔real gap.

    ``clean_contains_rate`` / ``real_contains_rate`` are the answerable-contains
    rates on the clean benchmark vs the messy real-usage set. The four probe
    fields are ``None`` when their probe was SKIPPED (live-only / MCP-unreachable).
    """

    clean_contains_rate: float
    real_contains_rate: float
    benchmark_real_gap_pp: float
    clean_strict_rate: Optional[float] = None
    real_strict_rate: Optional[float] = None
    real_source: str = "perturb"          # provenance: perturb | langfuse | qa
    real_n: int = 0                        # number of real-usage items scored
    clarity: Optional[ClarityMetrics] = None
    refuse: Optional[RefuseMetrics] = None
    recall_drop: Optional[RecallDrop] = None
    rewrite_drift: Optional[RewriteDrift] = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_metrics(self) -> dict:
        """Flat ``{metric_name: value|None}`` map for the gate's floor logic.

        Probe metrics that were not measured are ``None`` (honest skip, not a
        pass). The gate decides GO/NO-GO; this is just the measured surface.
        """
        clarity = self.clarity
        refuse = self.refuse
        return {
            "benchmark_real_gap_pp": self.benchmark_real_gap_pp,
            "real_contains_rate": self.real_contains_rate,
            "real_strict_rate": self.real_strict_rate,
            "clean_contains_rate": self.clean_contains_rate,
            "clarity_precision": clarity.precision if clarity else None,
            "clarity_recall": clarity.recall if clarity else None,
            "false_clarify_rate": clarity.false_clarify_rate if clarity else None,
            "false_answer_rate": clarity.false_answer_rate if clarity else None,
            "over_refuse_rate": refuse.over_refuse_rate if refuse else None,
            "under_refuse_rate": refuse.under_refuse_rate if refuse else None,
            "recall_drop_pp": self.recall_drop.drop_pp if self.recall_drop else None,
            "rewrite_term_drift_pp": self.rewrite_drift.drift_pp if self.rewrite_drift else None,
        }

    def as_dict(self) -> dict:
        """Nested, report-friendly view (probe sub-objects expanded or ``None``)."""
        return {
            "family": "real_usage",
            "headline": {
                "benchmark_real_gap_pp": self.benchmark_real_gap_pp,
                "max_gap_pp_advisory": MAX_GAP_PP_ADVISORY,
                "advisory_no_go": self.exceeds_gap_floor(),
            },
            "clean_contains_rate": self.clean_contains_rate,
            "clean_strict_rate": self.clean_strict_rate,
            "real_contains_rate": self.real_contains_rate,
            "real_strict_rate": self.real_strict_rate,
            "real_source": self.real_source,
            "real_n": self.real_n,
            "clarity": self.clarity.as_dict() if self.clarity else None,
            "refuse": self.refuse.as_dict() if self.refuse else None,
            "recall_drop": self.recall_drop.as_dict() if self.recall_drop else None,
            "rewrite_drift": self.rewrite_drift.as_dict() if self.rewrite_drift else None,
            "notes": list(self.notes),
        }

    def exceeds_gap_floor(self, max_gap_pp: float = MAX_GAP_PP_ADVISORY) -> bool:
        """Whether the benchmark↔real gap exceeds the (advisory) floor."""
        return self.benchmark_real_gap_pp > max_gap_pp


def from_scores(clean: ScoreResult, real: ScoreResult, *,
                real_source: str = "perturb",
                clarity: Optional[ClarityMetrics] = None,
                refuse: Optional[RefuseMetrics] = None,
                recall_drop: Optional[RecallDrop] = None,
                rewrite_drift: Optional[RewriteDrift] = None,
                notes: tuple[str, ...] = ()) -> RealUsageFamily:
    """Build the family from a clean and a real-usage :class:`ScoreResult`.

    ``clean`` is combined88 scored on its curated answers; ``real`` is the same
    questions perturbed (or Langfuse-mined / external QA) and scored against the
    **preserved** ground_truth. Optional probe outputs attach as-is.
    """
    return RealUsageFamily(
        clean_contains_rate=clean.contains_rate,
        clean_strict_rate=clean.strict_rate,
        real_contains_rate=real.contains_rate,
        real_strict_rate=real.strict_rate,
        benchmark_real_gap_pp=benchmark_real_gap_pp(clean.contains_rate, real.contains_rate),
        real_source=real_source,
        real_n=real.answerable_total,
        clarity=clarity,
        refuse=refuse,
        recall_drop=recall_drop,
        rewrite_drift=rewrite_drift,
        notes=notes,
    )
