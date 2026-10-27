"""Register a fixed rectified physical camera to the CARLA monitor plane.

Only calibration correspondences are used. No simulator object truth is sent to
perception or risk. Existing virtual-camera ground-plane specification supplies
metric scale; current screen capture determines the image-to-screen mapping.
"""
import argparse
import json
import os
from pathlib import Path
import time
import urllib.request
import cv2
import numpy as np

IDS=tuple(range(10,19))


def pattern(width=1920,height=1080):
    canvas=np.full((height,width,3),255,np.uint8)
    dictionary=cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    size=110
    centers=tuple((x,y) for y in (.53,.73,.92) for x in (.10,.50,.90))
    corners={}
    for ident,(nx,ny) in zip(IDS,centers):
        x,y=round(nx*width-size/2),round(ny*height-size/2)
        marker=cv2.aruco.generateImageMarker(dictionary,ident,size)
        canvas[y:y+size,x:x+size]=cv2.cvtColor(marker,cv2.COLOR_GRAY2BGR)
        corners[ident]=np.float32([[x,y],[x+size-1,y],[x+size-1,y+size-1],[x,y+size-1]])
    return canvas,corners


def frame(url):
    with urllib.request.urlopen(url,timeout=4) as response:
        data=b''
        while b'\xff\xd9' not in data:
            data+=response.read(16384)
            if len(data)>3_000_000:raise ValueError('Invalid frame stream')
        start=data.index(b'\xff\xd8');end=data.index(b'\xff\xd9',start)+2
    image=cv2.imdecode(np.frombuffer(data[start:end],np.uint8),cv2.IMREAD_COLOR)
    return cv2.resize(image,(1280,720))


def display():
    import pygame
    pygame.display.init()
    screen=pygame.display.set_mode((1920,1080),pygame.NOFRAME,display=1)
    image,_=pattern();surface=pygame.surfarray.make_surface(image[:,:,::-1].swapaxes(0,1))
    screen.blit(surface,(0,0));pygame.display.flip()
    while True:
        for event in pygame.event.get():
            if event.type==pygame.QUIT or (event.type==pygame.KEYDOWN and event.key==pygame.K_ESCAPE):return
        time.sleep(.05)


def calibrate(args):
    _,references=pattern()
    parameters=cv2.aruco.DetectorParameters()
    parameters.cornerRefinementMethod=cv2.aruco.CORNER_REFINE_SUBPIX
    detector=cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50),parameters)
    samples=[];best=None
    for attempt in range(24):
        if args.lossless_capture:
            from perception.capture_common import capture
            destination=args.output.parent/'registration-lossless'/('frame-'+str(time.time_ns())+'.png')
            capture('ground_'+str(time.time_ns()),destination);image=cv2.imread(str(destination))
        else:image=frame(args.url)
        corners,ids,_=detector.detectMarkers(image)
        found={} if ids is None else {int(i):c.reshape(4,2) for i,c in zip(ids.ravel(),corners)}
        print('markers',sorted(found),flush=True)
        if all(i in found for i in IDS):
            samples.append(np.concatenate([found[i] for i in IDS]))
            best=image
        if len(samples)>=8:break
        time.sleep(.15)
    if len(samples)<5:
        cv2.imwrite(str(args.output.with_suffix('.failed.jpg')),image)
        raise RuntimeError('All nine markers must be visible in at least five frames')
    camera=np.median(samples,axis=0).astype(np.float32)
    screen=np.concatenate([references[i] for i in IDS])
    h,mask=cv2.findHomography(screen,camera,cv2.RANSAC,2.5)
    errors=np.linalg.norm(cv2.perspectiveTransform(screen[None],h)[0]-camera,axis=1)
    holdout=[]
    for marker in range(len(IDS)):
        use=np.array([i//4!=marker for i in range(len(IDS)*4)])
        alternative,_=cv2.findHomography(screen[use],camera[use],0)
        predicted=cv2.perspectiveTransform(screen[~use][None],alternative)[0]
        holdout.extend(np.linalg.norm(predicted-camera[~use],axis=1).tolist())
    jitter=np.linalg.norm(np.asarray(samples)-camera,axis=2)
    report={'marker_ids':list(IDS),'frames':len(samples),'median_reprojection_px':float(np.median(errors)),
            'max_reprojection_px':float(max(errors)),'max_leave_one_marker_out_px':float(max(holdout)),
            'p95_corner_jitter_px':float(np.percentile(jitter,95)),
            'screen_to_rectified_camera':h.tolist()}
    if max(holdout)>6 or np.percentile(jitter,95)>2.5 or max(errors)>4:
        raise RuntimeError('Registration did not pass independent/jitter checks: '+json.dumps(report))
    virtual=json.loads(args.virtual.read_text())
    virtual_h=cv2.getPerspectiveTransform(np.float32(virtual['image_points']),np.float32(virtual['ground_points_m']))
    virtual_points=np.float32([[128,395],[1152,395],[1152,718],[128,718]])
    virtual['ground_points_m']=cv2.perspectiveTransform(virtual_points[None],virtual_h)[0].tolist()
    virtual['maximum_distance_m']=30
    points=virtual_points*np.float32([1920/virtual['image_size'][0],1080/virtual['image_size'][1]])
    image_points=cv2.perspectiveTransform(points[None],h)[0]
    config={**virtual,'image_size':[1280,720],'image_points':image_points.tolist(),
            'calibration_source':'Physical rectified OX08BC camera -> fixed CARLA monitor -> virtual ground plane',
            'registration':report,'created_time_ns':time.time_ns(),
            'limitations':['Valid only for this fixed physical camera/monitor/zoom configuration',
                           'Metric scale is CARLA virtual-camera specification; assumes flat virtual ground',
                           'Camera or monitor movement requires recalibration; no real-road metric claim']}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(config,indent=2)+'\n')
    cv2.imwrite(str(args.output.with_suffix('.capture.jpg')),best)
    print(json.dumps(report,indent=2),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--display',action='store_true')
    p.add_argument('--lossless-capture',action='store_true',help='Use opt-in Jetson capture requests instead of resized MJPEG')
    p.add_argument('--url',default='http://192.168.50.21:8080/stream.mjpg')
    p.add_argument('--virtual',type=Path,default=Path('config/camera/carla-front-rgb-ground.json'))
    p.add_argument('--output',type=Path,default=Path('config/camera/physical-screen-ground-20261027.json'))
    a=p.parse_args()
    if a.display:display()
    else:calibrate(a)
