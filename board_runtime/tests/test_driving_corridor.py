from dataclasses import replace
from pathlib import Path
import time
import numpy as np
import pytest
from codrive.driving_corridor import DrivingCorridor
from codrive.models import Header, VehicleState, SituationSnapshot, ExternalPerception, TrackedObject, BoundingBox
from codrive.risk import RiskEngine

CONFIG=Path('config/camera/carla-driving-corridor.json')
NOW=3_000_000_000_000


def make_obj(corridor, ident, x, z, kind='car'):
    p=np.linalg.inv(corridor.h)@np.array([x,z,1]);u,v=p[:2]/p[2]
    return TrackedObject(ident,kind,.95,BoundingBox(u-min(.01,u,1-u),v-.05,u+min(.01,u,1-u),v),
                         distance_m=float(np.hypot(x,z)),ttc_s=.5,in_path=True)


def snapshot(corridor, objects, *, steer=0, speed=10, now=NOW, sequence=1):
    h=Header.now('test',sequence,capture_time_ns=now)
    return SituationSnapshot(h,vehicle=VehicleState(h,speed,steer,False,20),
                             external=(ExternalPerception(h,'front','test',objects=tuple(objects)),))


def test_adjacent_lanes_and_sidewalk_never_create_collision_candidates():
    c=DrivingCorridor.load(CONFIG)
    objs=[make_obj(c,1,3.5,10),make_obj(c,2,-3.5,10),make_obj(c,3,2.8,10,'person')]
    # Even upstream in_path=True/TTC=.5 cannot override camera-ground corridor exclusion.
    e=RiskEngine(driving_corridor=c)
    assert e.evaluate(snapshot(c,objs),now_ns=NOW) is None
    assert c.status['accepted']==0 and c.status['filtered']==3


def test_own_lane_obstacle_retains_emergency_and_raw_ml_snapshot_is_unchanged():
    c=DrivingCorridor.load(CONFIG)
    s=snapshot(c,[make_obj(c,1,0,15),make_obj(c,2,3.5,15)])
    e=RiskEngine(driving_corridor=c);r=e.evaluate(s,now_ns=NOW)
    assert r.code=='object_collision' and e.assessment.stable_level.value=='EMERGENCY'
    assert len(s.external[0].objects)==2 and s.external[0].objects[0].ttc_s==.5
    assert len(e.snapshot.external[0].objects)==1
    assert e.snapshot.external[0].objects[0].ttc_s==pytest.approx((15-c.config['vehicle_front_m']-c.radius('car'))/10,abs=.002)


@pytest.mark.parametrize('steer',[65,-65])
def test_bicycle_path_follows_steering_instead_of_fixed_image_roi(steer):
    c=DrivingCorridor.load(CONFIG)
    v=snapshot(c,[],steer=steer).vehicle
    x,z,_=c.pose(12,c.curvature(v))
    objs=[make_obj(c,1,x,z),make_obj(c,2,0,20)]
    filtered=c.filter(snapshot(c,objs,steer=steer),now_ns=NOW)
    assert [o.track_id for o in filtered.external[0].objects]==[1]
    assert c.status['curvature_per_m']*steer>0


def test_constant_speed_lead_vehicle_stops_looking_like_stationary_obstacle():
    c=DrivingCorridor.load(CONFIG)
    for i in range(4):
        now=NOW+i*200_000_000
        frame=snapshot(c,[make_obj(c,1,0,15)],now=now,sequence=i+1)
        result=c.filter(frame,now_ns=now)
    assert result.external[0].objects==()


def test_pedestrian_crossing_into_path_is_retained_by_motion_prediction():
    c=DrivingCorridor.load(CONFIG)
    for i in range(4):
        now=NOW+i*200_000_000
        # Moving ego observes a pedestrian crossing from outside the static corridor.
        frame=snapshot(c,[make_obj(c,1,3-i*.4,12-i,'person')],now=now,sequence=i+1,speed=5)
        result=c.filter(frame,now_ns=now)
    assert c.tracks[('front',1,'person')]['velocity'][0] < 0
    assert result.external[0].objects[0].in_path
    assert 0 < result.external[0].objects[0].ttc_s < 2


def test_near_roi_excludes_far_objects_and_unmeasured_pixels():
    c=DrivingCorridor.load(CONFIG)
    c.filter(snapshot(c,[make_obj(c,1,0,45)]),now_ns=NOW)
    assert c.status['objects'][0]['reason']=='outside_near_roi'
    assert c.project(.5,.1) is None


def test_no_vehicle_or_time_skew_does_not_assume_straight_steering():
    c=DrivingCorridor.load(CONFIG)
    s=snapshot(c,[make_obj(c,1,0,10)])
    assert c.filter(replace(s,vehicle=None),now_ns=NOW).external[0].objects==()
    s=replace(s,vehicle=replace(s.vehicle,header=replace(s.vehicle.header,capture_time_ns=NOW-300_000_000)))
    assert c.filter(s,now_ns=NOW).external[0].objects==()


def test_guide_projection_matches_calibrated_ground_and_turns_with_steering():
    c=DrivingCorridor.load(CONFIG)
    p=c.image_point(0,10)
    x,z=c.project(p[0]/1280,p[1]/720)
    assert x==pytest.approx(0,abs=.01) and z==pytest.approx(10,abs=.01)
    right=snapshot(c,[],steer=65);c.filter(right,now_ns=NOW)
    a=c.status['guide']
    left=snapshot(c,[],steer=-65);c.filter(left,now_ns=NOW)
    b=c.status['guide']
    assert len(a['left'])>2 and len(b['right'])>2
    assert a['center'][-1][0]>640 and b['center'][-1][0]<640
    assert c.filter(replace(left,vehicle=None),now_ns=NOW).vehicle is None
    assert 'guide' not in c.status


def test_crossing_pedestrian_guide_uses_same_collision_and_observed_motion():
    c=DrivingCorridor.load(CONFIG)
    for i in range(4):
        now=NOW+i*200_000_000
        s=snapshot(c,[make_obj(c,1,3-i*.4,12-i,'person')],speed=5,now=now,sequence=i+1)
        result=c.filter(s,now_ns=now)
    target=c.status['guide']['targets'][0]
    assert target['ttc_s']==pytest.approx(result.external[0].objects[0].ttc_s,abs=.005)
    assert target['motion_end'][0]<target['point'][0]
    assert len(target['bbox'])==4


def test_stationary_bbox_jitter_cannot_create_an_approaching_object():
    c=DrivingCorridor.load(CONFIG)
    for i in range(24):
        now=NOW+i*100_000_000
        z=13+(.45 if i%2 else -.45)
        result=c.filter(snapshot(c,[make_obj(c,1,0,z)],speed=0,now=now,sequence=i+1),now_ns=now)
        assert result.external[0].objects==()
    assert c.tracks[('front',1,'car')]['velocity']==(0.,0.)


def test_sustained_oncoming_vehicle_is_detected_with_ego_stopped():
    c=DrivingCorridor.load(CONFIG)
    for i in range(9):
        now=NOW+i*100_000_000
        z=13-2.5*i*.1 + (.08 if i%2 else -.08)
        result=c.filter(snapshot(c,[make_obj(c,1,0,z)],speed=0,now=now,sequence=i+1),now_ns=now)
    assert result.external[0].objects[0].in_path
    assert 0<result.external[0].objects[0].ttc_s<4
    assert c.tracks[('front',1,'car')]['velocity'][1]<-2


def test_pedestrian_margin_expands_near_path_without_expanding_vehicle_roi():
    c=DrivingCorridor.load(CONFIG)
    people=[make_obj(c,1,1.9,12,'pedestrian'),make_obj(c,2,2.8,12,'pedestrian'),make_obj(c,3,2.4,12,'car')]
    c.config['pedestrian_safety_margin_m']=0.
    assert c.filter(snapshot(c,people,speed=5),now_ns=NOW).external[0].objects==()
    c.config['pedestrian_safety_margin_m']=.69
    result=c.filter(snapshot(c,people,speed=5),now_ns=NOW)
    assert [o.track_id for o in result.external[0].objects]==[1]
    assert c.status['pedestrian_warning_half_width_m']==pytest.approx(2.0343392634391786)


def test_delayed_image_uses_capture_aligned_can_history():
    c=DrivingCorridor.load(CONFIG)
    earlier=snapshot(c,[],now=NOW,speed=10)
    c.filter(earlier,now_ns=NOW)
    delayed=snapshot(c,[make_obj(c,1,0,15)],now=NOW+300_000_000,speed=10)
    delayed=replace(delayed,external=(replace(delayed.external[0],header=earlier.header),))
    result=c.filter(delayed,now_ns=NOW+300_000_000)
    assert result.external[0].objects[0].in_path


def test_full_width_direct_rgb_calibration_matches_camera_intrinsics():
    c=DrivingCorridor.load(CONFIG)
    # 1280px, horizontal FOV60, mount 1.8m forward / 1.6m high.
    focal=1280/(2*np.tan(np.deg2rad(30)))
    for x,z in [(-4,15),(4,15),(0,10),(-3,8)]:
        u=(640+focal*x/(z-1.8))/1280
        v=(360+focal*1.6/(z-1.8))/720
        assert c.project(u,v)==pytest.approx((x,z),abs=1e-6)
    assert c.project(.01,.9) is not None
    assert c.project(.5,.3) is None


def test_capture_aligned_lane_reaches_team_core_without_widening_skew():
    c=DrivingCorridor.load(CONFIG)
    e=RiskEngine(driving_corridor=c,external_ttl_ms=500,vehicle_ttl_ms=500)
    for i in range(14):
        now=NOW+i*100_000_000
        s=snapshot(c,[],now=now,speed=10,sequence=i+1)
        p=replace(s.external[0],header=replace(s.header,capture_time_ns=now-300_000_000),lane_offset_m=.9,lane_confidence=.95)
        e.evaluate(replace(s,external=(p,)),now_ns=now)
    assert e.core.policy.max_context_skew_ms==200
    assert not any('road:time_skew' in x for x in e.assessment.unavailable_rules)
    assert any(x.code=='lane_departure' for x in e.assessment.candidates)


def test_pedestrian_crossing_velocity_ready_after_three_consistent_samples():
    c=DrivingCorridor.load(CONFIG)
    for i in range(3):
        now=NOW+i*150_000_000
        c.filter(snapshot(c,[make_obj(c,1,2.8-i*.4,12-i*.75,'person')],speed=5,now=now),now_ns=now)
    assert c.tracks[('front',1,'person')]['velocity'][0]<-2


def test_ttc_is_remaining_time_at_decision_not_at_camera_capture():
    c=DrivingCorridor.load(CONFIG)
    s=snapshot(c,[],speed=10)
    c.filter(s,now_ns=NOW)
    delayed=snapshot(c,[make_obj(c,1,0,15,'person')],speed=10,now=NOW+300_000_000)
    delayed=replace(delayed,external=(replace(delayed.external[0],header=s.header),))
    result=c.filter(delayed,now_ns=NOW+300_000_000)
    expected=(15-c.config['vehicle_front_m']-c.radius('person'))/10-.3
    assert result.external[0].objects[0].ttc_s==pytest.approx(expected,abs=.002)
    assert c.status['objects'][0]['capture_age_ms']==300
