"""Associate multiple metric lane boundaries as one stable ego-lane pair.

No inference dependency. Sudden jumps are unknown measurements, not new lane
positions. Coherent ego-straddling pairs can be reacquired after lane changes.
"""
from dataclasses import dataclass
import math
import numpy as np


@dataclass
class Boundary:
    key: int
    coefficients: tuple
    confidence: float
    def at(self, z):
        return np.polyval(self.coefficients,z)


class LanePairTracker:
    def __init__(self, vehicle_model=None):
        self.vehicle_model=vehicle_model or {}
        self.previous = None
        self.last_ns = None
        self.pending = None
        self.motion = None
        self.status = {'state':'waiting','selected_slots':[]}

    def update_motion(self, speed_mps, steering_wheel_deg, stamp_ns):
        if all(math.isfinite(x) for x in (speed_mps,steering_wheel_deg)):
            curve=np.asarray(self.vehicle_model.get('steering_curve_kmh',[[0,1],[20,.9],[60,.8],[120,.7]]))
            scale=float(np.interp(speed_mps*3.6,*curve.T))
            angle=math.radians(max(-1,min(1,steering_wheel_deg/450))*self.vehicle_model.get('maximum_road_wheel_deg',70)*scale)
            self.motion=(max(0,speed_mps),math.tan(angle)/self.vehicle_model.get('wheelbase_m',3.005),stamp_ns)

    def predicted(self, stamp):
        if self.previous is None:return None
        dt=(stamp-self.last_ns)/1e9
        if not self.motion or not -200_000_000<=stamp-self.motion[2]<=300_000_000:
            return self.previous
        speed,curvature,_=self.motion
        distance=speed*dt;yaw=distance*curvature
        if abs(curvature)<1e-7:mx,mz=0,distance
        else:
            mx=(1-math.cos(yaw))/curvature+self.vehicle_model.get('rear_axle_offset_m',1.5)*math.sin(yaw)
            mz=math.sin(yaw)/curvature+self.vehicle_model.get('rear_axle_offset_m',1.5)*(math.cos(yaw)-1)
        result=[]
        for boundary in self.previous:
            z=np.linspace(4,15,24);x=boundary.at(z)
            dx,dz=x-mx,z-mz
            nx=dx*math.cos(yaw)-dz*math.sin(yaw)
            nz=dx*math.sin(yaw)+dz*math.cos(yaw)
            result.append(Boundary(boundary.key,tuple(np.polyfit(nz,nx,2)),boundary.confidence))
        return tuple(result)

    @staticmethod
    def error(a,b):
        z=np.array([5.,8.,11.])
        return float(np.sqrt(np.mean(np.concatenate([a[i].at(z)-b[i].at(z) for i in (0,1)])**2)))

    @staticmethod
    def straddles(pair):
        return pair[0].at(0)<0<pair[1].at(0)

    def update(self, boundaries, stamp):
        if self.last_ns is not None and not 0 <= stamp-self.last_ns <= 1_000_000_000:
            self.previous=self.pending=None
            self.last_ns=None
        z=np.array([4.,8.,12.]);pairs=[]
        for left in boundaries:
            for right in boundaries:
                if left.key==right.key:continue
                widths=right.at(z)-left.at(z)
                if np.min(widths)<2.6 or np.max(widths)>4.6 or np.ptp(widths)>.8:continue
                if not 2.4 < right.at(0)-left.at(0) < 4.8:continue
                pair=(left,right)
                if self.previous is None and not self.straddles(pair):continue
                pairs.append(pair)
        predicted=self.predicted(stamp)
        def score(pair):
            continuity=self.error(pair,predicted) if predicted is not None else abs((pair[0].at(0)+pair[1].at(0))/2)
            width=float(pair[1].at(8)-pair[0].at(8))
            return continuity+.2*abs(width-3.5)+.1*(2-pair[0].confidence-pair[1].confidence)
        pairs.sort(key=score)
        selected=pairs[0] if pairs else None
        error=self.error(selected,predicted) if selected and predicted else None
        offset_step=(abs(float((selected[0].at(0)+selected[1].at(0)-predicted[0].at(0)-predicted[1].at(0))/2))
                     if selected and predicted else None)
        accepted=selected is not None and predicted is not None and error<=.45 and offset_step<=.4
        if accepted:
            # Smooth only associated boundaries; never average across adjacent lanes.
            selected=tuple(Boundary(new.key,tuple(.5*np.array(new.coefficients)+.5*np.array(old.coefficients)),new.confidence)
                           for new,old in zip(selected,predicted))
            self.pending=None
        else:
            eligible=[p for p in pairs if self.straddles(p)]
            candidate=eligible[0] if eligible else None
            previous_pending=self.pending
            if candidate is None:
                self.pending=None
            elif previous_pending and 0<=stamp-previous_pending[2]<=750_000_000 and self.error(candidate,previous_pending[0])<=.3:
                self.pending=(candidate,previous_pending[1],stamp,previous_pending[3]+int(stamp>previous_pending[2]))
            else:
                self.pending=(candidate,stamp,stamp,1)
            confirm_ns=800_000_000 if self.previous is not None else 300_000_000
            if self.pending and self.pending[3]>=3 and stamp-self.pending[1]>=confirm_ns:
                selected=self.pending[0];accepted=True;self.pending=None
        if not accepted:
            self.status={'state':'reacquiring' if pairs else 'missing_pair',
                         'selected_slots':[],'candidates':len(boundaries),'step_error_m':error}
            return None
        self.previous=selected;self.last_ns=stamp
        centre=(selected[0].at(0)+selected[1].at(0))/2
        heading=math.degrees(math.atan((selected[0].coefficients[1]+selected[1].coefficients[1])/2))
        self.status={'state':'tracked','selected_slots':[b.key for b in selected],
                     'candidates':len(boundaries),'width_m':float(selected[1].at(8)-selected[0].at(8)),
                     'step_error_m':error,'offset_m':float(-centre),'heading_error_deg':heading}
        return float(-centre),min(b.confidence for b in selected),heading
