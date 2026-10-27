"""Bounded asynchronous model display; never publishes DDS warnings or speech."""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from .. import latency_probe as latency
from typing import Protocol

from ..models import SituationSnapshot
from .adapter import AdapterPolicy, adapt_snapshot

LOG = logging.getLogger(__name__)


class Predictor(Protocol):
    def predict(self, sample: dict) -> dict: ...


class ShadowRisk:
    def __init__(self, predictor: Predictor, *, policy: AdapterPolicy | None = None) -> None:
        self.predictor = predictor
        self.policy = policy or AdapterPolicy()
        self.contract_version = getattr(predictor, "contract_version", 2)
        self.final_adapter = None
        if self.contract_version == 3:
            from ..v3_feature_engine import RiskPolicy
            from .adapter_v3 import FinalSensorAdapter

            configured = getattr(predictor, "metadata", {}).get("rule_policy")
            self.final_adapter = FinalSensorAdapter(
                policy=RiskPolicy(**configured) if configured else None
            )
        self._condition = threading.Condition()
        self._pending: tuple | None = None
        self._active_key: str | None = None
        self._result_key: str | None = None
        self._result: dict = {}
        self._result_frame = None
        self._result_input_time_ns = 0
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name="xgboost-shadow", daemon=True)
        self._thread.start()

    def update(self, snapshot: SituationSnapshot, *, now_ns: int, features=None) -> dict:
        frame = (
            self.final_adapter.adapt(snapshot, now_ns=now_ns, features=features)
            if self.final_adapter
            else adapt_snapshot(snapshot, now_ns=now_ns, policy=self.policy)
        )
        key = json.dumps(frame.inputs, sort_keys=True, allow_nan=False)
        if self.contract_version == 3:
            from ..sensor_features import SENSOR_FIELDS

            usable = any(frame.inputs[name] is not None for name in SENSOR_FIELDS)
        else:
            usable = any(value is not None for value in frame.inputs.values())
        with self._condition:
            if not usable:
                self._pending = None
            elif key != self._result_key and key != self._active_key:
                # Replacing this single slot drops superseded observations, never the Pi tick.
                self._pending = (key, frame.inputs, frame, snapshot.header.capture_time_ns)
                self._condition.notify()
            exact = usable and key == self._result_key
            recent = False
            if self.contract_version == 3 and usable and self._result_frame is not None:
                from ..sensor_features import VALIDITY_FIELDS

                recent = (
                    self._result_input_time_ns <= now_ns < self._result_frame.valid_until_ns
                    and all(
                        frame.inputs[name] == self._result_frame.inputs[name]
                        for name in VALIDITY_FIELDS
                    )
                )
            result = (
                copy.deepcopy(self._result)
                if exact or recent
                else {
                    "status": "pending" if usable else "unavailable",
                    "xgboost_risk_level": None,
                    "risk_probability": None,
                    "warning_message": "판단 중"
                    if usable
                    else "유효한 센서 입력이 없어 판단할 수 없습니다.",
                    "degraded": True,
                }
            )
            # At 10 Hz a continuously changing stream rarely matches the exact
            # newest row. Display the last still-valid prediction with ITS OWN
            # feature frame and timestamp, never relabel it as the newest input.
            displayed = self._result_frame if recent and not exact else frame
            input_time = (
                self._result_input_time_ns
                if recent and not exact
                else snapshot.header.capture_time_ns
            )
        if self.contract_version == 3 and self.final_adapter is not None:
            from .adapter_v3 import sensor_missing_fields, LIMITED_INPUT_NOTICE
            unavailable=sensor_missing_fields(displayed,snapshot,now_ns=now_ns,
                policy=self.final_adapter.engine.policy)
            result['sensor_missing_fields']=list(unavailable)
            result['sensor_input_degraded']=bool(unavailable)
            if not unavailable and result.get('status')=='ok':
                message=result.get('warning_message','')
                if message.endswith(LIMITED_INPUT_NOTICE):
                    result['warning_message']=message[:-len(LIMITED_INPUT_NOTICE)]
        return {
            **result,
            "mode": "display_only",
            "feature_contract_version": self.contract_version,
            "inputs": displayed.inputs,
            "missing_fields": list(displayed.missing_fields),
            "withheld_sources": list(displayed.withheld_sources),
            "input_time_ns": input_time,
            "valid_until_ns": displayed.valid_until_ns,
            "latest_input_pending": bool(recent and not exact),
            "prediction_age_ms": max(0, now_ns - input_time) / 1e6,
        }

    def _run(self) -> None:
        last_started = 0.0
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopped or self._pending is not None)
                if self._stopped:
                    return
                # At most 10 Hz for v3; replace pending inputs while waiting.
                remaining = 0.1 - (time.monotonic() - last_started)
                if self.contract_version == 3 and remaining > 0:
                    self._condition.wait(timeout=remaining)
                    continue
                key, inputs, frame, input_time = self._pending
                self._pending = None
                self._active_key = key
                last_started = time.monotonic()
            trace = latency.begin("xgboost_worker", force=True)
            started = time.perf_counter_ns()
            try:
                result = self.predictor.predict(inputs)
            except Exception as exc:
                LOG.exception("XGBoost display inference failed")
                result = {
                    "status": "model_error",
                    "xgboost_risk_level": None,
                    "risk_probability": None,
                    "degraded": True,
                    "error": str(exc),
                    "warning_message": "모델 판단을 사용할 수 없습니다.",
                }
            result["inference_ms"] = (time.perf_counter_ns() - started) / 1e6
            if trace is not None:
                latency.link(input_time_ns=input_time, feature_key=str(key))
                latency.finish(trace)
            with self._condition:
                self._result_key, self._result = key, result
                self._result_frame, self._result_input_time_ns = frame, input_time
                self._active_key = None

    def close(self) -> None:
        with self._condition:
            self._stopped = True
            self._pending = None
            self._condition.notify()
        self._thread.join(timeout=1.0)
