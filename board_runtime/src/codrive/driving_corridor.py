"""Camera ground contacts against a steering-aware bicycle swept footprint.

Coordinates: x right, z forward, metres about the vehicle centre. No simulator
object truth is used. Only this decision snapshot is filtered; detections and ML
inputs remain intact. Based on ADAS_project's future-ego-footprint TTC approach.
"""
from dataclasses import replace
from collections import deque
import json
import math
from pathlib import Path
import numpy as np


class DrivingCorridor:
    def __init__(self, config, calibration):
        self.config = config
        self.tracks = {}
        self.motion_history = deque(maxlen=100)
        self.status = {'status':'waiting', 'objects':[]}
        for key in ('wheelbase_m','vehicle_half_width_m','vehicle_front_m','vehicle_rear_m',
                    'maximum_distance_m','horizon_s','step_s','steering_full_scale_deg',
                    'maximum_road_wheel_deg','steering_ratio','maximum_heading_rad'):
            if not math.isfinite(config[key]) or config[key] <= 0:
                raise ValueError('Invalid corridor setting: '+key)
        if not .05 <= config['step_s'] <= .25 or config['horizon_s'] > 6:
            raise ValueError('Corridor prediction must be bounded')
        if not 0 <= config['safety_margin_m'] <= .5 or not 0 <= config['rear_axle_offset_m'] <= config['wheelbase_m']:
            raise ValueError('Invalid vehicle margin/axle reference')
        margin=config.get('pedestrian_safety_margin_m',0.)
        if not math.isfinite(margin) or not 0<=margin<=1.:raise ValueError('Invalid pedestrian safety margin')
        size = np.asarray(calibration['image_size'], dtype=float)
        self.pixels = np.asarray(calibration['image_points'],dtype=float)/size
        target = np.asarray(calibration['ground_points_m'],dtype=float)
        a, b = [], []
        for (u,v),(x,z) in zip(self.pixels,target):
            a.extend([[u,v,1,0,0,0,-x*u,-x*v],[0,0,0,u,v,1,-z*u,-z*v]])
            b.extend([x,z])
        self.h = np.append(np.linalg.solve(a,b),1).reshape(3,3)
        self.inverse_h = np.linalg.inv(self.h)
        self.image_size = tuple(int(x) for x in size)
        self._guide_key = None
        self._guide_geometry = {}
        self._guide_targets = []
        self._guide_target_time = 0

    @classmethod
    def load(cls, path):
        path=Path(path)
        config=json.loads(path.read_text())
        calibration=json.loads((path.parent/config['calibration_file']).read_text())
        return cls(config,calibration)

    def project(self, u, v):
        p=np.array([u,v]);cross=[]
        for a,b in zip(self.pixels,np.roll(self.pixels,-1,axis=0)):
            edge=b-a;delta=p-a;cross.append(edge[0]*delta[1]-edge[1]*delta[0])
        if not (min(cross)>=-1e-9 or max(cross)<=1e-9):return None
        q=self.h@np.array([u,v,1])
        if abs(q[2])<1e-9:return None
        x,z=q[:2]/q[2]
        if not np.isfinite([x,z]).all() or z <= 0:return None
        return float(x),float(z)

    def image_point(self, x, z, *, drawing=False):
        # Clip the near singularity behind the virtual camera; SVG clips off-screen edges.
        if z <= 2.05:return None
        p=self.inverse_h@np.array([x,z,1.])
        if abs(p[2])<1e-9:return None
        u,v=p[:2]/p[2]
        if not np.isfinite([u,v]).all() or abs(u)>5 or abs(v)>5:return None
        if not drawing and self.project(float(u),float(v)) is None:return None
        return [round(float(u)*self.image_size[0],1),round(float(v)*self.image_size[1],1)]

    def guide_geometry(self, vehicle, curvature):
        key=(round(vehicle.speed_mps,2),round(curvature,5))
        if key==self._guide_key:return self._guide_geometry
        c=self.config;half=c['vehicle_half_width_m']+c['safety_margin_m']
        limit=min(c['maximum_distance_m'],max(15,vehicle.speed_mps*c['horizon_s']))
        left=[];right=[];centre=[];rungs=[]
        for distance in np.arange(0,limit+.01,.5):
            if abs(distance*curvature)>c['maximum_heading_rad']:break
            x,z,yaw=self.pose(float(distance),curvature)
            a=self.image_point(x-half*math.cos(yaw),z+half*math.sin(yaw),drawing=True)
            b=self.image_point(x+half*math.cos(yaw),z-half*math.sin(yaw),drawing=True)
            middle=self.image_point(x,z,drawing=True)
            # Rendering may extend off-screen; SVG clips it. Metric object admission
            # still uses the calibrated polygon and never extrapolates.
            if a:left.append(a)
            if b:right.append(b)
            if middle:centre.append(middle)
            if a and b and distance in (5,10,20,30):rungs.append({'left':a,'right':b,'distance_m':int(distance)})
        self._guide_key=key
        self._guide_geometry={'width':self.image_size[0],'height':self.image_size[1],
                              'left':left,'right':right,'center':centre,'rungs':rungs,
                              'preview':vehicle.speed_mps<1.4}
        return self._guide_geometry

    def curvature(self, vehicle):
        c=self.config
        if c['steering_model']=='carla_normalized':
            scale=float(np.interp(vehicle.speed_mps*3.6, *np.asarray(c['steering_curve_kmh']).T))
            road_deg=max(-1,min(1,vehicle.steering_wheel_deg/c['steering_full_scale_deg']))*c['maximum_road_wheel_deg']*scale
        elif c['steering_model']=='steering_ratio':
            road_deg=vehicle.steering_wheel_deg/c['steering_ratio']
        else:raise ValueError('Unknown steering model')
        return math.tan(math.radians(max(-70,min(70,road_deg))))/c['wheelbase_m']

    def pose(self, distance, curvature):
        yaw=distance*curvature
        if abs(curvature)<1e-7:return 0.,distance,0.
        radius=1/curvature;offset=self.config['rear_axle_offset_m']
        return (radius*(1-math.cos(yaw))+offset*math.sin(yaw),
                radius*math.sin(yaw)+offset*(math.cos(yaw)-1),yaw)

    def contains(self, point, pose, radius):
        x,z=point;cx,cz,yaw=pose;dx,dz=x-cx,z-cz
        lateral=dx*math.cos(yaw)-dz*math.sin(yaw)
        forward=dx*math.sin(yaw)+dz*math.cos(yaw)
        c=self.config
        return (abs(lateral)<=c['vehicle_half_width_m']+c['safety_margin_m']+radius
                and -c['vehicle_rear_m']-radius<=forward<=c['vehicle_front_m']+radius)

    def trajectory(self, speed, curvature):
        c=self.config
        result=[]
        for t in np.arange(0,c['horizon_s']+c['step_s']/2,c['step_s']):
            distance=speed*float(t)
            if distance>c['maximum_distance_m'] or abs(distance*curvature)>c['maximum_heading_rad']:break
            result.append((float(t),self.pose(distance,curvature)))
        return result

    def collision_time(self, point, velocity, speed, curvature, radius, trajectory=None):
        previous_t=0.
        for t,pose in self.trajectory(speed,curvature) if trajectory is None else trajectory:
            future=(point[0]+velocity[0]*t,point[1]+velocity[1]*t)
            if self.contains(future,pose,radius):
                # Refine the first sampled entry instead of quantising TTC to 0.1 s.
                low,high=previous_t,t
                for _ in range(6):
                    mid=(low+high)/2
                    if self.contains((point[0]+velocity[0]*mid,point[1]+velocity[1]*mid),self.pose(speed*mid,curvature),radius):high=mid
                    else:low=mid
                return high
            previous_t=t
        return None

    def radius(self,kind):
        base=.3 if kind in {'person','pedestrian','bicycle','motorcycle'} else .9
        return base+self.config.get('pedestrian_safety_margin_m',0.) if kind in {'person','pedestrian'} else base

    def velocity(self, key, point, stamp, vehicle, curvature):
        old=self.tracks.get(key)
        if old and old['stamp']==stamp:return old['velocity'],old['count']
        history=[];velocity=(0.,0.);count=1;residual=None
        if old and 0 < stamp-old['stamp'] <= 750_000_000:
            dt=(stamp-old['stamp'])/1e9
            distance=(vehicle.speed_mps+old['speed'])/2*dt
            mx,mz,yaw=self.pose(distance,(curvature+old['curvature'])/2)
            # Express older measurements in the current ego coordinate system.
            for t,(x,z) in old['history']:
                if stamp-t>750_000_000:continue
                dx,dz=x-mx,z-mz
                history.append((t,(dx*math.cos(yaw)-dz*math.sin(yaw),
                                   dx*math.sin(yaw)+dz*math.cos(yaw))))
        history.append((stamp,point));count=len(history)
        pedestrian=key[-1].lower() in {'person','pedestrian'}
        minimum_count,minimum_ns=(3,250_000_000) if pedestrian else (4,400_000_000)
        if count>=minimum_count and stamp-history[0][0]>=minimum_ns:
            times=np.asarray([(t-stamp)/1e9 for t,_ in history]);points=np.asarray([p for _,p in history])
            centered=times-times.mean();mean=points.mean(axis=0)
            fitted=(centered[:,None]*(points-mean)).sum(axis=0)/(centered@centered)
            errors=points-(mean+centered[:,None]*fitted)
            residual=float(np.sqrt(np.mean(np.sum(errors*errors,axis=1))))
            span=(stamp-history[0][0])/1e9
            displacement=float(np.linalg.norm(fitted))*span
            # BBOX footpoint jitter must not become an oncoming vehicle.
            # Require a sustained trend above the observed spatial noise floor.
            if (residual<=(.2 if pedestrian else .55) and displacement>=max(.4,3*residual)
                    and np.linalg.norm(fitted)<=35):
                velocity=tuple(map(float,fitted))
        self.tracks[key]={'point':point,'stamp':stamp,'speed':vehicle.speed_mps,
                          'curvature':curvature,'velocity':velocity,'count':count,
                          'history':history,'fit_residual_m':residual}
        return velocity,count

    def filter(self, snapshot, *, now_ns):
        vehicle=snapshot.vehicle
        if vehicle is None or not vehicle.can_valid:
            self.status={'status':'vehicle_unavailable','objects':[]}
            # Unknown steering must not be silently treated as a straight trajectory.
            return replace(snapshot,external=tuple(replace(p,objects=()) for p in snapshot.external))
        stamps=dict(vehicle.signal_times_ns)
        if stamps and any(name not in stamps or not 0 <= now_ns-stamps[name] <= 300_000_000
                          for name in ('speed_mps','steering_wheel_deg')):
            self.status={'status':'motion_signals_stale','objects':[]}
            return replace(snapshot,external=tuple(replace(p,objects=()) for p in snapshot.external))
        if not self.motion_history or vehicle.header.capture_time_ns > self.motion_history[-1].header.capture_time_ns:
            self.motion_history.append(vehicle)
        while self.motion_history and now_ns-self.motion_history[0].header.capture_time_ns>1_000_000_000:
            self.motion_history.popleft()
        c=self.config;curvature=self.curvature(vehicle);diagnostics=[];external=[];targets=[]
        for perception in snapshot.external:
            if perception.camera_id!=c['camera_id']:
                external.append(perception);continue
            # Match motion to image capture, not to network arrival. Retain the
            # original 200 ms alignment bound even for delayed CARLA images.
            image_vehicle=min(self.motion_history,key=lambda v:abs(perception.header.capture_time_ns-v.header.capture_time_ns),default=vehicle)
            if abs(perception.header.capture_time_ns-image_vehicle.header.capture_time_ns)>200_000_000:
                external.append(replace(perception,objects=()));continue
            image_curvature=self.curvature(image_vehicle)
            image_trajectory=self.trajectory(image_vehicle.speed_mps,image_curvature)
            objects=[]
            for obj in perception.objects:
                if obj.class_name.lower() not in {'person','pedestrian','car','vehicle','truck','bus','bicycle','motorcycle','obstacle'}:
                    continue
                point=self.project((obj.bbox.x1+obj.bbox.x2)/2,obj.bbox.y2)
                record={'track_id':obj.track_id,'class':obj.class_name,'in_path':False,'ttc_s':None}
                if point is None:
                    record['reason']='outside_metric_calibration';diagnostics.append(record);continue
                x,z=point;record.update(lateral_m=round(x,2),forward_m=round(z,2))
                if math.hypot(x,z)>c['maximum_distance_m']:
                    record['reason']='outside_near_roi';diagnostics.append(record);continue
                key=(perception.camera_id,obj.track_id,obj.class_name)
                velocity,count=self.velocity(key,point,perception.header.capture_time_ns,image_vehicle,image_curvature)
                record['velocity_mps']=[round(v,2) for v in velocity]
                record['fit_residual_m']=self.tracks[key].get('fit_residual_m')
                # Until temporal support exists, use stationary-object prediction only
                # while the ego is moving. Never invent motion for a parked vehicle.
                if count<3:velocity=(0.,0.)
                ttc=self.collision_time(point,velocity,image_vehicle.speed_mps,image_curvature,self.radius(obj.class_name.lower()),image_trajectory)
                if image_vehicle.speed_mps<1.4 and count<3:ttc=None
                if ttc is None:
                    record['reason']='outside_predicted_collision';diagnostics.append(record);continue
                capture_ttc=ttc
                age_s=max(0.,(now_ns-perception.header.capture_time_ns)/1e9)
                ttc=max(0.,ttc-age_s)
                record.update(capture_ttc_s=round(capture_ttc,3),capture_age_ms=round(age_s*1000,1))
                record.update(in_path=True,ttc_s=round(ttc,2),reason='swept_vehicle_intersection',velocity_samples=count)
                diagnostics.append(record)
                target={'track_id':obj.track_id,'class':obj.class_name,'ttc_s':round(ttc,2),
                        'point':self.image_point(x,z),
                        'bbox':[round(value*self.image_size[i%2],1) for i,value in enumerate((obj.bbox.x1,obj.bbox.y1,obj.bbox.x2,obj.bbox.y2))]}
                if obj.class_name.lower() in {'person','pedestrian','bicycle'} and count>=3 and abs(velocity[0])>.3:
                    target['motion_end']=self.image_point(x+velocity[0],z+velocity[1])
                targets.append(target)
                objects.append(replace(obj,distance_m=math.hypot(x,z),ttc_s=ttc,
                                       relative_speed_mps=None,in_path=True))
            external.append(replace(perception,objects=tuple(objects)))
        self.tracks={k:v for k,v in self.tracks.items() if now_ns-v['stamp']<=1_000_000_000}
        path=[self.pose(s,curvature)[:2] for s in np.arange(0,min(c['maximum_distance_m'],max(15,vehicle.speed_mps*c['horizon_s']))+.01,.5)
              if abs(s*curvature)<=c['maximum_heading_rad']]
        observed=[p.header.capture_time_ns for p in snapshot.external if p.camera_id==c['camera_id']]
        if observed:
            self._guide_targets=sorted(targets,key=lambda q:q['ttc_s'])[:3]
            self._guide_target_time=max(observed)
        guide=dict(self.guide_geometry(vehicle,curvature))
        guide['targets']=self._guide_targets if 0<=now_ns-self._guide_target_time<=500_000_000 else []
        guide['target_time_ns']=self._guide_target_time
        self.status={'guide':guide,'status':'active','steering_deg':vehicle.steering_wheel_deg,
                     'curvature_per_m':curvature,'maximum_distance_m':c['maximum_distance_m'],
                     'path_half_width_m':c['vehicle_half_width_m']+c['safety_margin_m'],
                     'pedestrian_warning_half_width_m':c['vehicle_half_width_m']+c['safety_margin_m']+self.radius('pedestrian'),
                     'path_m':path,'objects':diagnostics,'accepted':sum(x['in_path'] for x in diagnostics),
                     'filtered':sum(not x['in_path'] for x in diagnostics)}
        return replace(snapshot,external=tuple(external))
