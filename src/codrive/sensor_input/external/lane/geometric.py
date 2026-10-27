"""Color-supported left/right lane boundaries; at most one per side."""
from dataclasses import dataclass, replace
import json
from pathlib import Path
import time
import cv2
import numpy as np

@dataclass(frozen=True)
class LaneBoundary:
    side: str
    colour: str
    x_top: float
    x_bottom: float
    confidence: float
    pieces: int
    y_top: float
    y_bottom: float


def detect_lane_segments(frame, previous=None, config=None):
    previous = previous or {}
    config = config or json.loads((Path(__file__).parent / "geometric_weights.json").read_text())
    weights = config["weights"]
    image=cv2.resize(frame,(640,360),interpolation=cv2.INTER_AREA)
    h,w=image.shape[:2]
    hsv=cv2.cvtColor(image,cv2.COLOR_BGR2HSV)
    masks={'white':cv2.inRange(hsv,(0,0,170),(180,65,255)),
           'yellow':cv2.inRange(hsv,(15,85,100),(38,255,255))}
    roi=np.zeros((h,w),np.uint8)
    cv2.fillPoly(roi,[np.array([(int(.06*w),h-1),(int(.40*w),int(.55*h)),
                              (int(.60*w),int(.55*h)),(int(.94*w),h-1)],np.int32)],255)
    pieces=[]
    # Fit elongated painted regions, not arbitrary image edges.
    for colour,mask in masks.items():
        mask=cv2.morphologyEx(mask & roi,cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
        count,labels,stats,_=cv2.connectedComponentsWithStats(mask)
        for i in range(1,count):
            x,y,bw,bh,area=stats[i]
            if area<9 or bh<5 or bw>w*.25:continue
            yy,xx=np.where(labels[y:y+bh,x:x+bw]==i)
            points=np.column_stack((xx+x,yy+y)).astype(np.float32)
            centred=points-points.mean(axis=0)
            eigenvalues=np.linalg.eigvalsh(centred.T@centred/max(1,len(points)))
            if eigenvalues[-1]/max(1,eigenvalues[0])<4:continue
            vx,vy,px,py=cv2.fitLine(points,cv2.DIST_L2,0,.01,.01).ravel()
            if abs(vy)<.30:continue
            slope=float(vx/vy) # x = slope*y + intercept
            if not .12<=abs(slope)<=3.0:continue
            intercept=float(px-slope*py)
            bottom=(slope*(h*.90)+intercept)/w
            top=(slope*h*.56+intercept)/w
            side='left' if slope<0 else 'right'
            if side=='left' and not (.04<bottom<.49 and bottom<top<.64):continue
            if side=='right' and not (.51<bottom<.96 and .36<top<bottom):continue
            pieces.append({'side':side,'colour':colour,'slope':slope,'intercept':intercept,
                           'points':points,'bottom':bottom,'top':top,'range':(y,y+bh-1)})
    boundaries=[]
    for side in ('left','right'):
        available=[p for p in pieces if p['side']==side]
        candidates=[]
        for seed in available:
            group=[p for p in available if p['colour']==seed['colour']
                   and abs(p['bottom']-seed['bottom'])<.055
                   and abs(p['top']-seed['top'])<.05]
            pts=np.concatenate([p['points'] for p in group])
            rows=np.unique(pts[:,1]).size
            span=pts[:,1].max()-pts[:,1].min()
            # A long solid stripe, or multiple collinear dashes with vertical coverage.
            if rows<.04*h or span<.09*h:continue
            if len(group)==1 and rows<.10*h:continue
            vx,vy,px,py=cv2.fitLine(pts,cv2.DIST_HUBER,0,.01,.01).ravel()
            slope=float(vx/vy);intercept=float(px-slope*py)
            residual=np.abs(pts[:,0]-(slope*pts[:,1]+intercept))
            if np.percentile(residual,90)>14:continue
            top=(slope*h*.56+intercept)/w;bottom=(slope*(h*.90)+intercept)/w
            if side=='left' and not .04<bottom<.49:continue
            if side=='right' and not .51<bottom<.96:continue
            support=float(min(1,rows/(h*.20)))
            coverage=float(min(1,span/(h*.30)))
            alignment=float(max(0,1-np.mean(residual)/12))
            connections=float(min(1,len(group)/3))
            inner=float(max(0,1-abs(bottom-.5)/.45))
            temporal=0.5
            prior=previous.get(side)
            if prior is not None:
                prior_bottom=prior.x_top+(prior.x_bottom-prior.x_top)*(.90-prior.y_top)/(prior.y_bottom-prior.y_top)
                temporal=float(np.exp(-abs(bottom-prior_bottom)/.05))
            features=dict(support=support,coverage=coverage,alignment=alignment,connections=connections,inner=inner,temporal=temporal)
            confidence=sum(weights[k]*v for k,v in features.items())
            if confidence<config['min_score']:continue
            # Draw only between first and last supported paint; gaps are linked as one boundary.
            y0=float(pts[:,1].min());y1=float(pts[:,1].max())
            boundary=LaneBoundary(side,seed['colour'],(slope*y0+intercept)/w,
                                  (slope*y1+intercept)/w,confidence,len(group),y0/h,y1/h)
            candidates.append((confidence,bottom,top,boundary))
        if candidates:
            # Select one supported boundary per side using the weighted score.
            selected=max(candidates,key=lambda c:c[0])
            boundaries.append(selected[3])
    if len(boundaries)==2:
        left,right=boundaries
        # Check separation in a shared image row, not differently supported endpoints.
        def at(boundary,y):
            return boundary.x_top+(boundary.x_bottom-boundary.x_top)*(y-boundary.y_top)/(boundary.y_bottom-boundary.y_top)
        width=at(right,.95)-at(left,.95)
        if not .15<width<.90:
            boundaries=[max(boundaries,key=lambda b:b.confidence)]
    return boundaries


def draw_lane_segments(frame,boundaries):
    h,w=frame.shape[:2]
    for b in boundaries:
        colour=(0,220,255) if b.colour=='yellow' else (240,240,240)
        cv2.line(frame,(round(b.x_top*w),round(b.y_top*h)),
                 (round(b.x_bottom*w),round(b.y_bottom*h)),colour,3,cv2.LINE_AA)
        cv2.putText(frame,b.side,(round(b.x_bottom*w),min(h-10,round(b.y_bottom*h))),
                    cv2.FONT_HERSHEY_SIMPLEX,.55,colour,1,cv2.LINE_AA)


class GeometricLaneTracker:
    """Score current paint candidates using recent history; never invent missing sides."""
    def __init__(self):
        self.config=json.loads((Path(__file__).parent / "geometric_weights.json").read_text())
        self.previous={}
        self.last_time=0.0
    def detect(self,frame):
        now=time.monotonic()
        if now-self.last_time>self.config['history_ttl_seconds']:
            self.previous={}
        lanes=detect_lane_segments(frame,self.previous,self.config)
        smoothed=[]
        alpha=self.config['smoothing_alpha']
        for lane in lanes:
            prior=self.previous.get(lane.side)
            if prior and prior.colour==lane.colour:
                def x_at(y):
                    return prior.x_top+(prior.x_bottom-prior.x_top)*(y-prior.y_top)/(prior.y_bottom-prior.y_top)
                a,b=x_at(lane.y_top),x_at(lane.y_bottom)
                if max(abs(a-lane.x_top),abs(b-lane.x_bottom))<.08:
                    lane=replace(lane,x_top=alpha*lane.x_top+(1-alpha)*a,
                                 x_bottom=alpha*lane.x_bottom+(1-alpha)*b)
            smoothed.append(lane)
        self.previous={lane.side:lane for lane in smoothed}
        self.last_time=now
        return smoothed
