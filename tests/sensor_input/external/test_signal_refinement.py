import numpy as np
from codrive.contracts.models import TrafficLight as T
from codrive.sensor_input.driver.dms import YoloDetection as D
from codrive.sensor_input.external.signal_refinement import vertical_lamp_state,merge_signals,SignalRefinement


def test_three_lamp_position_survives_monitor_colour_washout():
    for index,state in enumerate((T.RED,T.YELLOW,T.GREEN)):
        frame=np.full((100,40,3),30,np.uint8)
        y=int(100*(.2,.5,.8)[index]);frame[y-10:y+10,12:28]=190
        result,evidence=vertical_lamp_state(frame,(0,0,40,100))
        assert result==state and evidence>.65


def test_uniform_billboard_and_two_bright_slots_are_unknown():
    frame=np.full((100,40,3),190,np.uint8)
    assert vertical_lamp_state(frame,(0,0,40,100))[0]==T.UNKNOWN
    frame[:]=30;frame[10:30,12:28]=190;frame[40:60,12:28]=185
    assert vertical_lamp_state(frame,(0,0,40,100))[0]==T.UNKNOWN


def test_crop_duplicate_does_not_duplicate_objects_or_remove_road_users():
    car=D('car',.8,(5,40,50,80));low=D('traffic light',.2,(5,5,15,30));full=D('traffic light',.7,(50,5,65,35));crop=D('traffic light',.8,(51,5,65,35))
    assert merge_signals((car,full,low),(crop,),.35)==(car,crop)


def test_wrong_resolution_rejected_instead_of_reusing_pixel_calibration():
    import pytest
    p=SignalRefinement({'signal_min_confidence':.35,'image_size':[1280,720],'signal_crops':[],'valid_image_polygon':[[0,0],[1280,0],[1280,720],[0,720]]})
    with pytest.raises(ValueError):p.crop_detections(np.zeros((100,200,3),np.uint8),None,640,None,False)


def test_reused_proposal_does_not_reuse_an_old_signal_colour():
    now=[1.];config={'signal_min_confidence':.4,'image_size':[640,384],'valid_image_polygon':[[0,0],[640,0],[640,384],[0,384]],'signal_crops':[[0,0,640,384]],'signal_detection_interval':2,'maximum_proposal_age_s':.18}
    ref=SignalRefinement(config,clock=lambda:now[0])
    class Box:
        cls=np.asarray([9]);conf=np.asarray([.8]);xyxy=np.asarray([[100,50,140,150]])
    class Result:
        names={9:'traffic_light'};boxes=Box()
    class Model:
        def predict(self,*_,**__):return [Result()]
    frame=np.full((384,640,3),30,np.uint8);frame[60:80,112:128]=190
    first=ref.crop_detections(frame,Model(),640,None,False)
    assert ref.state(frame,first[0].bbox_xyxy)[0]==T.RED
    frame[60:80,112:128]=30;frame[120:140,112:128]=190;now[0]+=.1
    second=ref.crop_detections(frame,Model(),640,None,False)
    assert ref.state(frame,second[0].bbox_xyxy)[0]==T.GREEN
