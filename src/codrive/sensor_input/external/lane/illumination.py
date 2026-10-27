"""Bounded dark-road brightening for lane inference, without changing coordinates."""
import cv2
import numpy as np

_GAMMA = np.array([round((i/255.)**.5*255) for i in range(256)], dtype=np.uint8)


def lane_input(frame):
    # Inspect road rather than sky / signal lamps. Keep daylight inputs untouched.
    h,w=frame.shape[:2]
    road=frame[h//2:,w//8:7*w//8]
    brightness=float(np.median(cv2.cvtColor(cv2.resize(road,(80,40)),cv2.COLOR_BGR2HSV)[:,:,2]))
    return cv2.LUT(frame,_GAMMA) if brightness<80 else frame
