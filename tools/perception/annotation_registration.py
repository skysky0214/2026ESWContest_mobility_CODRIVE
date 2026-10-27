"""Frame-local fiducial correction; rejects annotations if the markers are missing."""
import cv2
import numpy as np
from register_monitor import design

MARKERS={40:(160,120),41:(1500,120),42:(1500,900),43:(160,900)}


def registration(image,base):
    params=cv2.aruco.DetectorParameters();params.cornerRefinementMethod=cv2.aruco.CORNER_REFINE_SUBPIX
    params.adaptiveThreshWinSizeMax=73;params.adaptiveThreshWinSizeStep=4
    detector=cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50),params)
    corners,ids,_=detector.detectMarkers(image)
    found={} if ids is None else {int(i):c.reshape(4,2) for i,c in zip(ids.ravel(),corners)}
    if not all(i in found for i in MARKERS):raise ValueError('Four frame-local markers not visible')
    reference=[];observed=[]
    for ident,(x,y) in MARKERS.items():
        reference.extend([[x,y],[x+63,y],[x+63,y+63],[x,y+63]]);observed.extend(found[ident])
    reference=np.asarray(reference,np.float32);observed=np.asarray(observed,np.float32)
    prior=design(reference)@np.asarray(base['coefficients'])
    h,_=cv2.findHomography(prior.astype(np.float32),observed,0)
    error=np.linalg.norm(cv2.perspectiveTransform(prior.astype(np.float32)[None],h)[0]-observed,axis=1)
    if max(error)>3:raise ValueError('Frame marker residual too high: '+str(max(error)))
    return {'correction':h.tolist(),'marker_error_max_px':float(max(error))}


def project(points,base,local):
    camera=design(np.asarray(points,dtype=float))@np.asarray(base['coefficients'])
    return cv2.perspectiveTransform(camera.astype(np.float32)[None],np.asarray(local['correction']))[0]


def box_to_camera(box,base,local):
    x1,y1,x2,y2=box
    points=[[x*1.5,y*1.5] for x in (x1,(x1+x2)/2,x2) for y in (y1,(y1+y2)/2,y2)]
    camera=project(points,base,local);a=camera.min(axis=0);b=camera.max(axis=0)
    return [float(a[0]),float(a[1]),float(b[0]),float(b[1])]


def screen_polygon(base,local):
    points=([[x,0] for x in range(0,1921,120)]+[[1920,y] for y in range(120,1081,120)]+
            [[x,1080] for x in range(1800,-1,-120)]+[[0,y] for y in range(960,0,-120)])
    return project(points,base,local)
