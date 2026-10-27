"""Local decision pipeline; future sensor adapters only need to call ingest()."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ...situation.fusion import SituationFusion
from ...guidance.policy import HmiPolicy
from ...contracts.models import (
    AudioEvent, DriverState, EnvironmentState, ExternalPerception, HmiCommand,
    InsState, RiskAssessment, SituationSnapshot, VehicleState,
)
from ...risk.engine import RiskEngine


SensorSample = (
    AudioEvent | DriverState | EnvironmentState | ExternalPerception | InsState | VehicleState
)


@dataclass(frozen=True, slots=True)
class PipelineResult:
    snapshot: SituationSnapshot
    assessment: RiskAssessment
    hmi: HmiCommand | None
    rejected_samples: int


class DecisionPipeline:
    """Run fusion, scoring, priority, and FSM without DDS or hardware dependencies."""

    def __init__(
        self, *, fusion: SituationFusion | None = None,
        risk_engine: RiskEngine | None = None, hmi_policy: HmiPolicy | None = None,
    ) -> None:
        self.fusion = fusion or SituationFusion()
        self.risk_engine = risk_engine or RiskEngine(freshness=self.fusion.policy)
        self.hmi_policy = hmi_policy or HmiPolicy()
        self.rejected_samples = 0

    def ingest(self, sample: SensorSample, *, received_time_ns: int | None = None) -> bool:
        accepted = self.fusion.ingest(sample, received_time_ns=received_time_ns)
        if not accepted:
            self.rejected_samples += 1
        return accepted

    def step(self, *, now_ns: int | None = None) -> PipelineResult:
        now_ns = time.time_ns() if now_ns is None else now_ns
        snapshot = self.fusion.snapshot(now_ns=now_ns)
        assessment = self.risk_engine.assess(snapshot, now_ns=now_ns)
        command = self.hmi_policy.render(assessment.event) if assessment.event else None
        return PipelineResult(snapshot, assessment, command, self.rejected_samples)
