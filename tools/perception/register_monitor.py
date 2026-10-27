"""Dense marker registration used only for annotation transfer, never model input."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import cv2
import numpy as np
from capture_common import capture

IDS=list(range(20,36))


def pattern():
    width,height=1920,1080;canvas=np.full((height,width,3),255,np.uint8);points={};size=100
    dictionary=cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for ident,(x,y) in zip(IDS,((x,y) for y in (.1,.36,.64,.9) for x in (.08,.36,.64,.92))):
        u,v=round(x*width-size/2),round(y*height-size/2)
        marker=cv2.aruco.generateImageMarker(dictionary,ident,size)
        canvas[v:v+size,u:u+size]=cv2.cvtColor(marker,cv2.COLOR_GRAY2BGR)
        points[ident]=np.float32([[u,v],[u+size-1,v],[u+size-1,v+size-1],[u,v+size-1]])
    return canvas,points


def design(points):
    x=points[:,0]/1920*2-1;y=points[:,1]/1080*2-1
    return np.column_stack([np.ones(len(x)),x,y,x*x,x*y,y*y,x*x*x,x*x*y,x*y*y,y*y*y])


def display():
    import pygame
    pygame.display.init();screen=pygame.display.set_mode((1920,1080),pygame.NOFRAME,display=1)
    image,_=pattern();screen.blit(pygame.surfarray.make_surface(image[:,:,::-1].swapaxes(0,1)),(0,0));pygame.display.flip()
    while True:
        for e in pygame.event.get():
            if e.type==pygame.QUIT:return
        time.sleep(.05)


def main(a):
    _,reference=pattern();params=cv2.aruco.DetectorParameters();params.cornerRefinementMethod=cv2.aruco.CORNER_REFINE_SUBPIX
    detector=cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50),params)
    camera=[];out=a.output.parent;out.mkdir(parents=True,exist_ok=True)
    for i in range(20):
        path=out/f'registration-{i:02d}.png';capture('reg_'+str(time.time_ns()),path)
        image=cv2.imread(str(path));corners,ids,_=detector.detectMarkers(image)
        found={} if ids is None else {int(k):c.reshape(4,2) for k,c in zip(ids.ravel(),corners)}
        print('markers',len(found),flush=True)
        if all(k in found for k in IDS):camera.append(np.concatenate([found[k] for k in IDS]))
        if len(camera)>=6:break
    if len(camera)<4:raise RuntimeError('All 16 markers must be visible')
    image=np.median(camera,axis=0);screen=np.concatenate([reference[k] for k in IDS]);x=design(screen)
    coefficients=np.linalg.lstsq(x,image,rcond=None)[0]
    error=np.linalg.norm(x@coefficients-image,axis=1);leave=[]
    for k in range(len(IDS)):
        keep=np.array([j//4!=k for j in range(len(screen))]);c=np.linalg.lstsq(x[keep],image[keep],rcond=None)[0]
        leave.extend(np.linalg.norm(x[~keep]@c-image[~keep],axis=1).tolist())
    jitter=np.linalg.norm(np.asarray(camera)-image,axis=2)
    report={'screen_size':[1920,1080],'camera_size':[1280,720],'model':'normalized_cubic_screen_to_camera',
      'coefficients':coefficients.tolist(),'marker_count':16,'frames':len(camera),
      'median_error_px':float(np.median(error)),'max_error_px':float(max(error)),
      'max_holdout_error_px':float(max(leave)),'p95_jitter_px':float(np.percentile(jitter,95)),
      'purpose':'Offline ground-truth projection only; no CARLA actor truth injected into perception'}
    if max(leave)>3 or np.percentile(jitter,95)>1.5:raise RuntimeError('Registration failed: '+json.dumps(report))
    a.output.write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--display',action='store_true');p.add_argument('--output',type=Path,default=Path('build/perception-eval-20261027/monitor-registration.json'));a=p.parse_args()
    if a.display:display()
    else:main(a)
