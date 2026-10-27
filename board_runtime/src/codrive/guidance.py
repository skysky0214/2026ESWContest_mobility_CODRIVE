"""Deterministic HMI policy; remote language models are never safety-critical."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .models import Header, HmiCommand, RiskEvent, Severity


@dataclass(frozen=True, slots=True)
class _Template:
    display: str
    speech: str
    icon: str


TEMPLATES: dict[str, _Template] = {
    "object_brake": _Template("충돌 위험! 제동", "전방 충돌 위험입니다. 즉시 제동하세요.", "warning"),
    "object_slow": _Template("속도를 줄이세요", "전방 위험 요소가 있습니다. 속도를 줄이세요.", "warning"),
    "yellow_light_slow": _Template("황색 신호 주의", "전방 황색 신호입니다. 속도를 줄이세요.", "traffic_light"),
    "check_surroundings": _Template("주변을 확인하세요", "주변 위험 요소를 확인하세요.", "warning"),
    "phone_observation": _Template("휴대폰 사용 감지", "휴대폰 사용이 감지되었습니다. 휴대폰을 내려놓고 전방을 주시하세요.", "phone"),
    "drowsy_observation": _Template("졸음 징후 감지", "졸음 징후가 감지되었습니다. 안전한 장소에 정차하고 휴식하세요.", "eyes"),
    "attention_observation": _Template("주의 분산 감지", "주의 분산이 감지되었습니다. 전방을 주시하세요.", "eyes"),
    "pedestrian_brake": _Template(
        "보행자! 즉시 제동", "전방 보행자 위험입니다. 즉시 제동하세요.", "pedestrian"
    ),
    "lane_return": _Template(
        "차선으로 복귀", "차선을 벗어나고 있습니다. 안전하게 복귀하세요.", "lane"
    ),
    "red_light_brake": _Template(
        "빨간불! 제동", "전방 적색 신호입니다. 제동하세요.", "traffic_light"
    ),
    "look_forward": _Template("전방을 보세요", "위험합니다. 전방을 확인하세요.", "eyes"),
    "siren_yield": _Template(
        "긴급차량 양보", "긴급차량이 접근합니다. 주변을 확인하고 양보하세요.", "siren"
    ),
    "release_accelerator": _Template(
        "가속페달 떼세요", "급가속이 감지되었습니다. 가속 페달에서 발을 떼세요.", "pedal"
    ),
}


# Retained extension phrases; authoritative decisions remain in the team core.
EXTRA_TEMPLATES = {
    "full_accelerator": _Template("가속페달을 확인하세요", "가속페달 입력이 큽니다. 가속페달을 확인하세요.", "pedal"),
    "stop_sign": _Template("정지 표지판 확인", "정지 표지판이 감지되었습니다. 주변을 확인하세요.", "warning"),
    "road_users": _Template("주변 보행자·차량 확인", "주변 보행자와 차량을 확인하세요.", "warning"),
    "head_drop_observation": _Template("고개 떨굼 감지", "고개 떨굼이 감지되었습니다. 전방을 확인하세요.", "eyes"),
    "rapid_acceleration": _Template("급가속 감지", "급가속이 감지되었습니다. 가속 페달을 천천히 놓으세요.", "pedal"),
    "rapid_pedal": _Template("급격한 가속페달 조작", "가속 페달을 급하게 밟았습니다. 부드럽게 조작하세요.", "pedal"),
    "siren_left": _Template("왼쪽 사이렌 감지", "왼쪽에서 사이렌이 감지되었습니다. 주변을 확인하세요.", "siren"),
    "siren_right": _Template("오른쪽 사이렌 감지", "오른쪽에서 사이렌이 감지되었습니다. 주변을 확인하세요.", "siren"),
    "horn_left": _Template("왼쪽 경적 감지", "왼쪽에서 경적이 감지되었습니다. 주변을 확인하세요.", "warning"),
    "horn_right": _Template("오른쪽 경적 감지", "오른쪽에서 경적이 감지되었습니다. 주변을 확인하세요.", "warning"),
    "siren_unknown": TEMPLATES["siren_yield"],
    "horn_unknown": _Template("경적 감지", "경적이 감지되었습니다. 주변을 확인하세요.", "warning"),
}

class HmiPolicy:
    """Convert a validated risk into a bounded, immediately usable command."""

    def __init__(self, source_id: str = "pi5.hmi-policy") -> None:
        self.source_id = source_id
        self._sequence = 0

    def render(self, event: RiskEvent) -> HmiCommand:
        template = {**EXTRA_TEMPLATES, **TEMPLATES}.get(
            event.message_key,
            _Template("주변을 확인하세요", "주변 위험 요소를 확인하세요.", "warning"),
        )
        self._sequence += 1
        display_ms = 3_000 if event.severity is Severity.CRITICAL else 5_000
        return HmiCommand(
            header=Header.now(
                self.source_id,
                self._sequence,
                frame_id="hmi",
                capture_time_ns=event.header.capture_time_ns,
                trace_id=event.header.trace_id,
            ),
            event_id=event.event_id,
            risk_code=event.code,
            severity=event.severity,
            display_text=template.display,
            tts_text=template.speech,
            icon=template.icon,
            display_ms=display_ms,
            source="template",
            expires_at_ns=event.expires_at_ns,
        )


@dataclass(frozen=True, slots=True)
class HmiDecision:
    command: HmiCommand
    speak: bool


@dataclass(slots=True)
class HmiArbiter:
    """Reject stale/backlogged commands and suppress overlapping repeated TTS."""

    repeat_speech_gap_ms: int = 3_000
    _active: HmiCommand | None = None
    _seen_event_ids: set[str] = field(default_factory=set)
    _last_spoken_by_code: dict[str, int] = field(default_factory=dict)

    def clear_active(self) -> None:
        """Release delivery priority after the authoritative FSM changes state."""
        self._active = None

    def accept(self, command: HmiCommand, *, now_ns: int | None = None,
               allow_speech: bool = True) -> HmiDecision | None:
        now_ns = time.time_ns() if now_ns is None else now_ns
        if command.expires_at_ns <= now_ns or command.event_id in self._seen_event_ids:
            return None
        if (
            self._active is not None
            and self._active.expires_at_ns > now_ns
            and command.severity < self._active.severity
        ):
            return None

        previous_severity = self._active.severity if self._active is not None else None
        last_spoken = self._last_spoken_by_code.get(command.risk_code)
        gap_elapsed = (
            last_spoken is None or now_ns - last_spoken >= self.repeat_speech_gap_ms * 1_000_000
        )
        speak = allow_speech and (gap_elapsed or (
            previous_severity is not None and command.severity > previous_severity
        ))
        if speak:
            self._last_spoken_by_code[command.risk_code] = now_ns
        self._active = command
        self._seen_event_ids.add(command.event_id)
        if len(self._seen_event_ids) > 4_096:
            self._seen_event_ids = {command.event_id}
        return HmiDecision(command=command, speak=speak)
