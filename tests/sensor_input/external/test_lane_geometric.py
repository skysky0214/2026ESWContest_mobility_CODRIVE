import cv2
import numpy as np
from codrive.sensor_input.external.lane.geometric import detect_lane_segments

def scene(): return np.full((720,1280,3),45,np.uint8)
def stripe(f,side,colour,dashed=False,outer=False):
    sign=-1 if side=='left' else 1
    def point(y): return (round(640+sign*((y-380)*(.85 if outer else .65)+40)),y)
    if dashed:
        for y in (425,500,575,650): cv2.line(f,point(y),point(y+35),colour,10)
    else: cv2.line(f,point(415),point(710),colour,10)
def test_white_yellow_dashed():
    f=scene();stripe(f,'left',(255,255,255),True);stripe(f,'right',(0,255,255),True)
    b=detect_lane_segments(f)
    assert [(x.side,x.colour) for x in b]==[('left','white'),('right','yellow')]
    assert all(x.pieces>=2 for x in b)
def test_one_per_side():
    f=scene()
    for side in ('left','right'):
        stripe(f,side,(255,255,255));stripe(f,side,(0,255,255),outer=True)
    b=detect_lane_segments(f)
    assert len(b)==2
    assert len({x.side for x in b})==2
    assert all(x.colour=='white' for x in b)
def test_single_side_does_not_invent_other():
    f=scene();stripe(f,'left',(255,255,255),True)
    assert [x.side for x in detect_lane_segments(f)]==['left']
def test_unpainted_edges_rejected():
    f=scene();stripe(f,'left',(100,100,100));stripe(f,'right',(255,0,0))
    assert detect_lane_segments(f)==[]
def test_horizontal_crosswalk_rejected():
    f=scene()
    for y in range(430,700,65):cv2.rectangle(f,(200,y),(1080,y+20),(255,255,255),-1)
    assert detect_lane_segments(f)==[]


def test_tracker_clears_missing_paint():
    from codrive.sensor_input.external.lane.geometric import GeometricLaneTracker
    tracker=GeometricLaneTracker()
    f=scene();stripe(f,'left',(255,255,255),True)
    assert len(tracker.detect(f))==1
    assert tracker.detect(scene())==[]
    assert tracker.previous=={}

def test_oblique_dashes():
    f=scene()
    for y in (430,480,530):
        cv2.line(f,(round(650+2*(y-420)),y),(round(650+2*(y+20-420)),y+20),(255,255,255),8)
    lanes=detect_lane_segments(f)
    assert [b.side for b in lanes]==['right']
    assert lanes[0].pieces>=2
