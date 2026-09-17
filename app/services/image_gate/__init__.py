"""Hard image quality gate — fail closed, identity veto, max 3 attempts."""
from .refs import load_active_reference_set, ReferenceSetError, ActiveReferenceSet
from .scorer import score_candidate, ScorerError, ScoreCard
from .gate import (
    assert_pass_for_heygen,
    record_score,
    evaluate_pass,
    run_score_image_step,
    run_revise_image_step,
    GateRefuse,
    rewrite_prompt,
)

__all__ = [
    "load_active_reference_set",
    "ReferenceSetError",
    "ActiveReferenceSet",
    "score_candidate",
    "ScorerError",
    "ScoreCard",
    "assert_pass_for_heygen",
    "record_score",
    "evaluate_pass",
    "run_score_image_step",
    "run_revise_image_step",
    "GateRefuse",
    "rewrite_prompt",
]
