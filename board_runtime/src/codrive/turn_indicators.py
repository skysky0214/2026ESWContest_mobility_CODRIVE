"""Edge-triggered G29 indicator switches, independent of vehicle motion controls."""
from .models import TurnSignal


class IndicatorSwitch:
    def __init__(self, left_button=5, right_button=4):
        if left_button == right_button:
            raise ValueError('Indicator buttons must be distinct')
        self.left_button, self.right_button = left_button, right_button
        self.state = TurnSignal.OFF
        self.pressed = set()

    def update(self, button, pressed):
        if not pressed:
            self.pressed.discard(button)
            return self.state
        if button in self.pressed:
            return self.state
        self.pressed.add(button)
        target = {self.left_button:TurnSignal.LEFT, self.right_button:TurnSignal.RIGHT}.get(button)
        if target is not None:
            self.state = TurnSignal.OFF if self.state == target else target
        return self.state

    def reset(self):
        self.pressed.clear()
        self.state = TurnSignal.OFF
