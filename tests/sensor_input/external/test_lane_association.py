import pytest
from codrive.sensor_input.external.lane.association import LanePairTracker,Boundary

T=3_000_000_000_000
MS=1_000_000


def boundaries(shift=0):
    return [Boundary(i,(0,0,x+shift),.9) for i,x in enumerate([-5.25,-1.75,1.75,5.25])]


def initialized():
    tracker=LanePairTracker()
    assert tracker.update(boundaries(),T) is None
    assert tracker.update(boundaries(),T+200*MS) is None
    assert tracker.update(boundaries(),T+400*MS)[0]==pytest.approx(0)
    return tracker


def test_all_four_candidates_select_only_stable_ego_pair():
    t=initialized();assert t.status['selected_slots']==[1,2]


def test_adjacent_lane_jump_does_not_publish_false_lateral_offset():
    t=initialized()
    assert t.update([Boundary(1,(0,0,-5.25),.99),Boundary(2,(0,0,1.75),.99)],T+600*MS) is None
    assert t.update([Boundary(1,(0,0,-5.25),.99),Boundary(2,(0,0,-1.75),.99)],T+800*MS) is None
    assert t.status['selected_slots']==[]
    assert t.update(boundaries(),T+900*MS)[0]==pytest.approx(0)


def test_small_jitter_is_smoothed_without_binding_by_model_slot():
    t=initialized()
    lanes=[Boundary(b.key+10,b.coefficients,b.confidence) for b in boundaries(.1)]
    result=t.update(lanes,T+600*MS)
    assert result[0]==pytest.approx(-.05)
    assert t.status['selected_slots']==[11,12]


def test_missing_pair_and_long_gap_require_fresh_reacquisition():
    t=initialized();assert t.update([],T+600*MS) is None
    assert t.update(boundaries(),T+1600*MS) is None
    assert t.update(boundaries(),T+1800*MS) is None
    assert t.update(boundaries(),T+2000*MS) is not None


def test_duplicate_frame_does_not_satisfy_three_observations():
    t=LanePairTracker()
    assert t.update(boundaries(),T) is None
    assert t.update(boundaries(),T+300*MS) is None
    assert t.update(boundaries(),T+300*MS) is None
    assert t.update(boundaries(),T+400*MS) is not None


def test_curve_pair_is_accepted_and_width_mismatch_is_unknown():
    t=LanePairTracker();lanes=[Boundary(1,(.01,.02,-1.75),.9),Boundary(2,(.01,.02,1.75),.9)]
    for i in range(3):result=t.update(lanes,T+i*200*MS)
    assert result is not None and result[0]==pytest.approx(0)
    assert t.update([lanes[0],Boundary(2,(.01,.02,5.25),.9)],T+600*MS) is None


def test_ground_projection_publishes_unknown_on_lane_pair_jump():
    from types import SimpleNamespace
    import numpy as np
    from codrive.sensor_input.external.ground_geometry import GroundGeometry
    from codrive.contracts.models import Header,ExternalPerception
    g=GroundGeometry.load('config/camera/carla-front-rgb-ground.json')
    inv=np.linalg.inv(g.h)
    def lane(slot,lateral):
        coords=[]
        for z in np.linspace(5,15,25):
            p=inv@np.array([lateral,z,1]);xy=p[:2]/p[2]/g.size
            coords.append(tuple(xy))
        return SimpleNamespace(slot_id=slot,side='candidate',points=coords,confidence=.9)
    lanes=[lane(i,x) for i,x in enumerate([-5.25,-1.75,1.75,5.25])]
    for i in range(3):
        h=Header.now('test',i+1,capture_time_ns=T+i*200*MS)
        result=g.enrich(ExternalPerception(h,'front','test'),lanes)
    assert result.lane_offset_m==pytest.approx(0,abs=.001)
    assert g.selected_slots==[1,2]
    h=Header.now('test',4,capture_time_ns=T+600*MS)
    result=g.enrich(ExternalPerception(h,'front','test'),[lane(1,-5.25),lane(2,1.75)])
    assert result.lane_offset_m is None and result.lane_confidence==0


def test_crossing_pedestrian_keeps_identity_when_narrow_boxes_do_not_overlap():
    import numpy as np
    from codrive.sensor_input.external.ground_geometry import GroundGeometry
    from codrive.contracts.models import Header,ExternalPerception,TrackedObject,BoundingBox
    g=GroundGeometry.load('config/camera/carla-front-rgb-ground.json');inv=np.linalg.inv(g.h)
    ids=[]
    for i in range(4):
        p=inv@np.array([2-i*.5,12,1]);u,v=p[:2]/p[2]/g.size
        obj=TrackedObject(0,'person',.95,BoundingBox(u-.005,v-.08,u+.005,v))
        h=Header.now('test',i+1,capture_time_ns=T+i*300_000_000)
        result=g.enrich(ExternalPerception(h,'front','test',objects=(obj,)))
        ids.append(result.objects[0].track_id)
    assert len(set(ids))==1
