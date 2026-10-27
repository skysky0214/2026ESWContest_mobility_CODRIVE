from codrive.sensor_input.external.traffic_signal import TrafficSignalTracker
from codrive.contracts.models import TrafficLight as T

def candidate(state=T.GREEN,box=(.48,.1,.52,.2)):
    return (box,state,.2,.9)

def test_confirmation_and_change():
    tracker=TrafficSignalTracker()
    assert tracker.update([candidate()])[0]==T.UNKNOWN
    assert tracker.update([candidate()])[0]==T.UNKNOWN
    assert tracker.update([candidate()])[0]==T.GREEN
    assert tracker.update([candidate(T.RED)])[0]==T.UNKNOWN
    assert tracker.update([])==(T.UNKNOWN,0.0)

def test_central_candidate_over_side_red():
    tracker=TrafficSignalTracker()
    candidates=[candidate(),candidate(T.RED,(.16,.1,.20,.2))]
    for _ in range(3): state,_=tracker.update(candidates)
    assert state==T.GREEN

def test_gap_resets_confirmation():
    timestamp=[1.0]
    tracker=TrafficSignalTracker(clock=lambda:timestamp[0])
    for _ in range(3): tracker.update([candidate()])
    timestamp[0]+=1
    assert tracker.update([candidate()])[0]==T.UNKNOWN

def test_new_target_resets_confirmation():
    tracker=TrafficSignalTracker()
    for _ in range(3): tracker.update([candidate()])
    assert tracker.update([candidate(box=(.7,.1,.75,.2))])[0]==T.UNKNOWN
