"""Shared boundaries. Listings are data, never execution authority."""
from dataclasses import dataclass, field
from typing import Any, NamedTuple

EVALUATOR_VERSION = "generic-v1"


class CaptureBatch(NamedTuple):
    items: list[dict[str, Any]]
    coverage: dict[str, Any]


@dataclass(frozen=True)
class AnalysisInput:
    monitor_id: str
    criteria_hash: str
    evaluator_version: str
    item_revision: str
    item: dict[str, Any]
    criteria: dict[str, Any]
    baselines: list[dict[str, Any]] = field(default_factory=list)
    evidence_gaps: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AnalysisResult:
    decision: str
    reason: str
    comparison: str = ""
    evidence_gaps: list[str] = field(default_factory=list)
    evidence_used: list[str] = field(default_factory=list)
    confidence: float = 0.0
    analysis_source: str = "ai-text"


class AnalysisError(RuntimeError):
    """Retryable analysis failure; must not advance assessed fingerprints."""


class RuntimePreparationError(RuntimeError):
    """Safe, host-owned failure before collection; never contains raw diagnostics."""

    def __init__(self, code):
        if code not in {"browser_unavailable", "browser_timeout", "browser_config_mismatch"}:
            raise ValueError("invalid_preparation_error")
        super().__init__(code)

