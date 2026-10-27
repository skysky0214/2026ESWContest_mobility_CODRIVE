"""Image-space signal selection and conservative temporal confirmation."""
import time
from ...contracts.models import TrafficLight

class TrafficSignalTracker:
    def __init__(self, clock=time.monotonic, *, min_detection_confidence=.60, min_colour_support=.10):
        self.clock = clock
        self.min_detection_confidence = min_detection_confidence
        self.min_colour_support = min_colour_support
        self.last_time = 0.0
        self.box = None
        self.pending = TrafficLight.UNKNOWN
        self.count = 0
        self.selected_bbox = None

    def update(self, candidates):
        now = self.clock()
        # Candidates: normalized bbox tuple, colour, colour support, YOLO confidence.
        eligible = [c for c in candidates if .15 <= (c[0][0]+c[0][2])/2 <= .85
                    and (c[0][1]+c[0][3])/2 < .70
                    and c[3] >= self.min_detection_confidence
                    and c[2] >= self.min_colour_support
                    and c[1] != TrafficLight.UNKNOWN]
        if not eligible:
            self.box = self.selected_bbox = None
            self.pending = TrafficLight.UNKNOWN
            self.count = 0
            self.last_time = now
            return TrafficLight.UNKNOWN, 0.0
        def score(c):
            b = c[0]
            centre = (b[0]+b[2])/2
            area = (b[2]-b[0])*(b[3]-b[1])
            continuity = 0.0
            if self.box and now-self.last_time <= .3:
                distance = sum(abs(a-z) for a,z in zip(b,self.box))
                continuity = .3*max(0,1-distance/.2)
            return .55*(1-abs(centre-.5)/.35)+.15*min(1,area/.01)+.3*c[3]+continuity
        box, colour, support, confidence = max(eligible,key=score)
        same = self.box is not None and sum(abs(a-b) for a,b in zip(box,self.box)) < .2
        if not same or now-self.last_time > .3 or colour != self.pending:
            self.count = 0
        self.box = self.selected_bbox = box
        self.pending = colour
        self.last_time = now
        self.count += 1
        if colour == TrafficLight.UNKNOWN or self.count < 3:
            return TrafficLight.UNKNOWN, 0.0
        # Colour support remains a pixel fraction, not a calibrated probability.
        return colour, support
