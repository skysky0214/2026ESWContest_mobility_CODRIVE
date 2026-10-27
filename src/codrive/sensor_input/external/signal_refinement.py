"""Opt-in native-resolution signal crops and vertical-lamp evidence.

The vertical layout is explicit calibration, never inferred from simulator truth.
Evidence is contrast between lamp slots, not a calibrated model probability.
"""
from __future__ import annotations
import json
import time
from pathlib import Path
import cv2
import numpy as np
from ..driver.dms import YoloDetection
from ...contracts.models import TrafficLight


def overlap(a,b):
    intersection=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
    return intersection/max(1e-9,(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection)


def merge_signals(base,extra,minimum):
    others=[p for p in base if p.label!='traffic_light']
    lights=sorted([p for p in (*base,*extra) if p.label=='traffic_light' and p.confidence>=minimum and p.bbox_xyxy],key=lambda p:p.confidence,reverse=True)
    keep=[]
    for p in lights:
        if not any(overlap(p.bbox_xyxy,q.bbox_xyxy)>.4 for q in keep):keep.append(p)
    return tuple(others+keep)


def vertical_lamp_state(frame,box):
    h,w=frame.shape[:2];x1,y1,x2,y2=map(int,box)
    x1,y1,x2,y2=max(0,x1),max(0,y1),min(w,x2),min(h,y2)
    bh,bw=y2-y1,x2-x1
    if bh<12 or bw<5 or not 1.5<=bh/bw<=5.5:return TrafficLight.UNKNOWN,0.
    roi=cv2.cvtColor(frame[y1:y2,x1:x2],cv2.COLOR_BGR2GRAY)
    fits=[];patch_values={}
    # Fit small detector-box offsets; require a single bright slot and two dark slots.
    for cx in (.4,.5,.6):
        for shift in (-.06,0.,.06):
            for spacing in (.27,.30,.33):
                values=[]
                for position in (.5-spacing+shift,.5+shift,.5+spacing+shift):
                    top=max(0,int(bh*(position-.055)));bottom=min(bh,max(top+1,int(bh*(position+.055))))
                    left=max(0,int(bw*(cx-.10)));right=min(bw,max(left+1,int(bw*(cx+.10))))
                    key=(top,bottom,left,right)
                    if key not in patch_values:patch_values[key]=float(np.median(roi[top:bottom,left:right]))
                    values.append(patch_values[key])
                order=np.argsort(values);best,runner=values[order[-1]],values[order[-2]]
                contrast=best-runner
                if best>=70 and contrast>=30:fits.append((contrast/max(1.,best),int(order[-1])))
    if not fits:return TrafficLight.UNKNOWN,0.
    evidence,index=max(fits)
    rival=max((e for e,i in fits if i!=index),default=0.)
    if evidence<.25 or rival>=evidence-.10:return TrafficLight.UNKNOWN,0.
    return (TrafficLight.RED,TrafficLight.YELLOW,TrafficLight.GREEN)[index],min(1.,evidence)



class SignalRefinement:
    def __init__(self,config,clock=time.monotonic):
        self.clock=clock;self.last_proposals=();self.last_proposal_time=None;self.proposal_calls=0
        self.auxiliary_model=None
        if config.get('signal_layout','vertical_three_lamp')!='vertical_three_lamp':raise ValueError('Unsupported calibrated signal layout')
        self.config=config;self.minimum=float(config['signal_min_confidence'])
        self.polygon=np.asarray(config['valid_image_polygon'],np.float32)
        if self.polygon.ndim!=2 or self.polygon.shape[1]!=2:raise ValueError('Expected image polygon Nx2')
        if not 0<self.minimum<=1:raise ValueError('Invalid signal confidence')
    @classmethod
    def load(cls,path):return cls(json.loads(Path(path).read_text()))
    def contains(self,box):
        return cv2.pointPolygonTest(self.polygon,((box[0]+box[2])/2,(box[1]+box[3])/2),False)>=0
    def filter(self,detections):return tuple(p for p in detections if p.bbox_xyxy and self.contains(p.bbox_xyxy))
    def crop_detections(self,frame,model,imgsz,device,verbose):
        from ..driver.dms import detections_from_ultralytics
        extra=[];h,w=frame.shape[:2]
        auxiliary=self.config.get('signal_model')
        if auxiliary:
            if self.auxiliary_model is None:
                from ultralytics import YOLO
                self.auxiliary_model=YOLO(auxiliary,task='detect')
            model=self.auxiliary_model
        class_ids=[0] if auxiliary else [9]
        expected=self.config['image_size']
        if [w,h]!=expected:raise ValueError('Perception calibration image size mismatch')
        now=self.clock();interval=int(self.config.get('signal_detection_interval',1))
        self.proposal_calls+=1
        if (interval>1 and self.proposal_calls%interval!=1 and self.last_proposal_time is not None
                and 0<=now-self.last_proposal_time<=float(self.config.get('maximum_proposal_age_s',.18))):
            # Reuse spatial proposals only. Lamp state/evidence is always measured on this frame.
            return self.last_proposals
        for x1,y1,x2,y2 in self.config['signal_crops']:
            results=model.predict(frame[y1:y2,x1:x2],classes=class_ids,imgsz=imgsz,device=device,verbose=verbose,conf=.15)
            for p in (() if not results else detections_from_ultralytics(results[0])):
                if p.bbox_xyxy:
                    a,b,c,d=p.bbox_xyxy;extra.append(YoloDetection('traffic_light' if auxiliary else p.class_name,p.confidence,(a+x1,b+y1,c+x1,d+y1)))
        self.last_proposals=tuple(extra);self.last_proposal_time=now
        return self.last_proposals
    def state(self,frame,box):return vertical_lamp_state(frame,box)
