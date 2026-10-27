"""Presentation lifetime only: never changes risk events, FSM or speech scheduling."""
from dataclasses import replace


class WarningDisplay:
    def __init__(self, minimum_ms=2000):
        self.minimum_ns = minimum_ms * 1_000_000
        self.shown = None
        self.since_ns = 0
        self.had_speech = False
        self.preempted_speech = None
        self.speech_id = None
        self.speech_since_ns = 0

    def update(self, command, speaking, *, now_ns):
        if command is not None and command.expires_at_ns <= now_ns:
            command = None
        speech_id = speaking.event_id if speaking else None
        if speech_id != self.speech_id:
            self.speech_id, self.speech_since_ns = speech_id, now_ns
            self.preempted_speech = None
        # Match the playback worker's 15s timeout; disconnected players cannot pin alerts forever.
        if speaking and now_ns - self.speech_since_ns >= 15_000_000_000:
            speaking = None
        if speaking and command and command.severity > speaking.severity:
            self.preempted_speech = speaking.event_id
        eligible_speech = speaking and speaking.event_id != self.preempted_speech
        selected = speaking if eligible_speech else command
        if selected is None and self.shown and not self.had_speech:
            if now_ns - self.since_ns < self.minimum_ns:
                selected = self.shown
        if selected is None:
            self.shown = None
            return None
        identity = lambda item: (item.risk_code, item.display_text, item.severity)
        if self.shown is None or identity(selected) != identity(self.shown):
            self.since_ns, self.had_speech = now_ns, False
        self.shown = selected
        self.had_speech |= bool(eligible_speech and selected.event_id == speaking.event_id)
        return replace(selected, expires_at_ns=max(selected.expires_at_ns, now_ns + 1_500_000_000))
