import numpy as np
from codrive.sensor_input.external.lane.illumination import lane_input


def test_dark_input_is_brighter_without_geometry_change():
    frame=np.full((720,1280,3),25,np.uint8)
    frame[500:600,600:610]=60
    result=lane_input(frame)
    assert result.shape==frame.shape
    assert result[500,600,0]>60
    assert np.array_equal(result[:,:,0]>100,frame[:,:,0]>50)


def test_daylight_is_unmodified():
    frame=np.full((720,1280,3),150,np.uint8)
    assert lane_input(frame) is frame
