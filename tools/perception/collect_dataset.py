"""Stationary CARLA scene resets + real OX08BC captures + semantic visibility GT.

Simulator truth stays in this evaluator. The edge receives capture IDs only.
Own evaluation actors/sensor are removed and original world settings restored.
"""
import argparse
import json
import math
from pathlib import Path
import subprocess
import threading
import time
import urllib.request
import cv2
import numpy as np
import carla
from capture_common import capture,ssh,REMOTE,ADDRESS,CONTROL
from register_monitor import design

ROOT=Path(__file__).resolve().parents[2]
OWN='codrive_perception_evaluation'
class CaptureComplete(Exception):pass
OBJECTS=[('car','vehicle.audi.a2'),('pedestrian','walker.pedestrian.0001'),
 ('bicycle','vehicle.diamondback.century'),('motorcycle','vehicle.kawasaki.ninja'),
 ('bus','vehicle.mitsubishi.fusorosa'),('truck','vehicle.carlamotors.carlacola')]
TAGS={'pedestrian':[12,13],'car':[14],'truck':[15,14],'bus':[16,14],'bicycle':[19],'motorcycle':[18]}


def transform_json(t):
 return {'x':t.location.x,'y':t.location.y,'z':t.location.z,'pitch':t.rotation.pitch,'yaw':t.rotation.yaw,'roll':t.rotation.roll}


def class_name(actor):
 name=actor.type_id
 if name.startswith('walker.'):return 'pedestrian'
 if name in ('vehicle.diamondback.century','vehicle.gazelle.omafiets','vehicle.bh.crossbike'):return 'bicycle'
 if any(t in name for t in ('kawasaki','yamaha','harley','vespa')):return 'motorcycle'
 if 'fusorosa' in name:return 'bus'
 if any(t in name for t in ('carlacola','european_hgv','firetruck')):return 'truck'
 return 'car'


class GroundTruth:
 def __init__(self,world,camera,registration):
  self.world=world;self.camera=camera;self.coefficients=np.asarray(registration['coefficients'])
  self.semantic=None;self.lock=threading.Lock();self.sensor=None
 def receive(self,image):
  pixels=np.frombuffer(image.raw_data,np.uint8).reshape(image.height,image.width,4)[:,:,2].copy()
  with self.lock:self.semantic=(image.frame,image.timestamp,pixels)
 def project(self,vertices):
  inverse=np.asarray(self.camera.get_transform().get_inverse_matrix())
  xyz=np.asarray([[p.x,p.y,p.z,1] for p in vertices]);q=(inverse@xyz.T).T[:,:3]
  if min(q[:,0])<=.5:return None
  f=640/math.tan(math.radians(float(self.camera.attributes['fov']))/2)
  pixels=np.column_stack([640+f*q[:,1]/q[:,0],360-f*q[:,2]/q[:,0]])
  x1,y1=pixels.min(axis=0);x2,y2=pixels.max(axis=0)
  if x2<=0 or y2<=0 or x1>=1280 or y1>=720:return None
  return [max(0,int(x1)),max(0,int(y1)),min(1279,int(math.ceil(x2))),min(719,int(math.ceil(y2)))]
 def transfer(self,box):
  x1,y1,x2,y2=box
  points=np.asarray([[x,y] for x in (x1,(x1+x2)/2,x2) for y in (y1,(y1+y2)/2,y2)])*1.5
  mapped=design(points)@self.coefficients
  lo=mapped.min(axis=0);hi=mapped.max(axis=0)
  return [max(0,float(lo[0])),max(0,float(lo[1])),min(1280,float(hi[0])),min(720,float(hi[1]))]
 def annotate(self,primary):
  with self.lock:semantic=self.semantic
  if semantic is None:raise RuntimeError('Semantic GT camera not ready')
  frame,stamp,mask=semantic;labels=[];ignored=[]
  boxes=[]
  for actor in self.world.get_actors():
   if actor.id==self.camera.parent.id:continue
   if actor.type_id.startswith(('vehicle.','walker.')):
    kind=class_name(actor);boxes.append((kind,actor.id,actor.bounding_box,actor.get_transform(),None))
   elif actor.type_id=='traffic.traffic_light':
    for i,b in enumerate(actor.get_light_boxes()):
     if b.extent.z<.3:continue  # Exclude pedestrian push buttons.
     boxes.append(('traffic_light',str(actor.id)+':'+str(i),b,carla.Transform(),str(actor.get_state()).lower()))
  for kind,ident,b,t,state in boxes:
   center=t.transform(carla.Location(b.location.x,b.location.y,b.location.z))
   distance=center.distance(self.camera.get_location())
   if distance>110:continue
   projected=self.project(b.get_world_vertices(t))
   if projected is None:continue
   x1,y1,x2,y2=projected;region=mask[y1:y2+1,x1:x2+1]
   classes=[7] if kind=='traffic_light' else TAGS[kind]
   visible=np.isin(region,classes);ys,xs=np.where(visible)
   if len(xs)<2:
    ignored.append({'class':kind,'id':ident,'reason':'occluded_or_not_semantically_visible','virtual_bbox':projected,'bbox':self.transfer(projected)})
    continue
   tight=[x1+int(xs.min()),y1+int(ys.min()),x1+int(xs.max())+1,y1+int(ys.max())+1]
   box=self.transfer(tight)
   label={'class':kind,'id':ident,'bbox':box,'virtual_bbox':tight,'distance_m':distance,
          'state':state,'primary_signal':kind=='traffic_light' and str(ident).split(':')[0]==str(primary),
          'visible_semantic_pixels':int(len(xs))}
   if box[2]-box[0]<2 or box[3]-box[1]<4:
    label['reason']='below_reliable_pixel_size';ignored.append(label)
   else:labels.append(label)
  return {'labels':labels,'ignored':ignored,'semantic_frame':frame,'semantic_timestamp':stamp,
          'camera_transform':transform_json(self.camera.get_transform())}


def service(name):
 return subprocess.run(['systemctl','--user','is-active','--quiet',name]).returncode==0


def main(args):
 out=ROOT/args.output;out.mkdir(parents=True,exist_ok=False)
 registration=json.loads((ROOT/'build/perception-eval-20261027/monitor-registration.json').read_text())
 client=carla.Client('127.0.0.1',2000);client.set_timeout(5);world=client.get_world();road=world.get_map()
 ego=next(a for a in world.get_actors().filter('vehicle.*') if a.attributes.get('role_name')=='hero')
 if ego.get_velocity().length()>=.5 or ego.get_control().throttle>.03:raise RuntimeError('Stop ego first')
 camera=next(a for a in world.get_actors().filter('sensor.camera.rgb') if a.attributes.get('role_name')=='rgb_front_hd')
 original_pose=ego.get_transform();original_control=ego.get_control();original_weather=world.get_weather()
 lights=list(world.get_actors().filter('traffic.traffic_light'));original_lights=[(a,a.get_state(),a.is_frozen()) for a in lights]
 g29_active=service('codrive-carla-g29');scenario_active=service('codrive-carla-scenarios')
 try:current_scene=json.load(urllib.request.urlopen('http://127.0.0.1:18081/api/state',timeout=1))['scenario_id']
 except Exception:current_scene='pedestrian-lane'
 metadata={'original_pose':transform_json(original_pose),'g29_active':g29_active,'scenario_active':scenario_active,
           'original_scenario':current_scene,'registration':registration,'source':'lossless physical rectified camera PNG',
           'ground_truth':'Projected CARLA bounding boxes tightened by independent semantic sensor; no truth fed to perception',
           'split':'Intersection 22 held out for signal test; intersection 11 distance 25/60 validation; 15/40/80 development'}
 (out/'session.json').write_text(json.dumps(metadata,indent=2)+'\n')
 truth=GroundTruth(world,camera,registration);owned=[];records=[];run_prefix='r'+str(int(time.time()))
 def clear():
  for a in owned:
   if a.is_alive:a.destroy()
  owned.clear()
 def hold(pose):
  ego.set_target_velocity(carla.Vector3D());ego.set_target_angular_velocity(carla.Vector3D())
  ego.apply_control(carla.VehicleControl(brake=1,hand_brake=True));ego.set_transform(pose)
 def shot(case,primary,split):
  time.sleep(1.8)
  for k in range(3):
   ident=f'{run_prefix}_{len(records):04d}_{case}';gt=truth.annotate(primary)
   result=capture(ident)
   records.append({'id':ident,'case':case,'split':split,'image':ident+'.png',
                   'gt':gt,'baseline':result,'primary_signal_id':primary})
   with (out/'annotations.jsonl').open('a') as f:f.write(json.dumps(records[-1])+'\n')
   time.sleep(.12)
  print('CAPTURED',case,'frames',len(records),flush=True)
  if args.max_frames and len(records)>=args.max_frames:raise CaptureComplete()
 try:
  if scenario_active:subprocess.run(['systemctl','--user','stop','codrive-carla-scenarios'],check=True)
  if g29_active:subprocess.run(['systemctl','--user','stop','codrive-carla-g29'],check=True)
  subprocess.run(['ssh','-o','ControlPath=none','rp@192.168.99.21','systemctl --user stop codrive-pi'],check=True)
  ego.apply_control(carla.VehicleControl(brake=1,hand_brake=True))
  bp=world.get_blueprint_library().find('sensor.camera.semantic_segmentation')
  for key,value in {'role_name':OWN,'image_size_x':'1280','image_size_y':'720','fov':camera.attributes['fov'],'sensor_tick':'.2'}.items():bp.set_attribute(key,value)
  relative=np.asarray(ego.get_transform().get_inverse_matrix())@np.array([camera.get_location().x,camera.get_location().y,camera.get_location().z,1])
  cpose=camera.get_transform();epose=ego.get_transform()
  mount=carla.Transform(carla.Location(*map(float,relative[:3])),carla.Rotation(pitch=cpose.rotation.pitch-epose.rotation.pitch,yaw=cpose.rotation.yaw-epose.rotation.yaw,roll=cpose.rotation.roll-epose.rotation.roll))
  truth.sensor=world.spawn_actor(bp,mount,attach_to=ego);truth.sensor.listen(truth.receive)
  for a,_,_ in original_lights:a.freeze(True);a.set_state(carla.TrafficLightState.Red)
  for intersection in args.intersections:
   primary=world.get_actor(intersection);stop=(next(p for p in primary.get_stop_waypoints() if (p.road_id,p.lane_id)==((7,2) if intersection==11 else (4,-2))) if intersection in (11,22) else primary.get_stop_waypoints()[0])
   head=max(primary.get_light_boxes(),key=lambda b:b.location.z).location
   direction=stop.transform.get_forward_vector();ahead=(head.x-stop.transform.location.x)*direction.x+(head.y-stop.transform.location.y)*direction.y
   for weather_name in args.weathers:
    weather=world.get_weather();weather.cloudiness=10 if weather_name=='clear_day' else 95
    weather.precipitation=0 if weather_name=='clear_day' else 80;weather.wetness=0 if weather_name=='clear_day' else 100
    weather.precipitation_deposits=weather.precipitation;weather.sun_altitude_angle=45 if weather_name=='clear_day' else -20
    world.set_weather(weather)
    for distance in args.distances:
     choices=stop.previous(distance-ahead) if distance>ahead else stop.next(ahead-distance)
     wp=min(choices,key=lambda w:abs((w.transform.rotation.yaw-stop.transform.rotation.yaw+180)%360-180))
     pose=wp.transform;pose.location.z+=.08;hold(pose)
     split='test' if intersection!=11 else ('validation' if distance in (25,60) else 'development')
     for name,color in (('red',carla.TrafficLightState.Red),('yellow',carla.TrafficLightState.Yellow),('green',carla.TrafficLightState.Green)):
      primary.set_state(color);shot(f'signal_{intersection}_{weather_name}_{distance}_{name}',intersection,split)
  # Independent object cases, using the same visible road and semantic visibility checks.
  world.set_weather(original_weather)
  stop=next(p for p in world.get_actor(11).get_stop_waypoints() if (p.road_id,p.lane_id)==(7,2))
  origin=stop.previous(65)[0].transform;origin.location.z+=.08;hold(origin)
  for kind,blueprint in OBJECTS:
   for distance in (12,18,25):
    clear();bp=world.get_blueprint_library().find(blueprint);bp.set_attribute('role_name',OWN)
    location=origin.transform(carla.Location(x=distance,z=1 if kind=='pedestrian' else .5))
    actor=world.try_spawn_actor(bp,carla.Transform(location,origin.rotation))
    if actor is None:print('SPAWN BLOCKED',blueprint,distance,flush=True);continue
    owned.append(actor)
    if kind=='pedestrian':actor.apply_control(carla.WalkerControl(speed=0))
    else:actor.apply_control(carla.VehicleControl(brake=1,hand_brake=True))
    shot(f'object_{kind}_{distance}',11,{12:'development',18:'validation',25:'test'}[distance])
  clear()
  for yaw in (-50,50,120):
   pose=carla.Transform(origin.location,carla.Rotation(yaw=origin.rotation.yaw+yaw));hold(pose)
   shot(f'background_{yaw+180}',None,'test' if yaw==120 else 'validation')
 except CaptureComplete:
  pass
 finally:
  clear()
  if truth.sensor and truth.sensor.is_alive:truth.sensor.stop();truth.sensor.destroy()
  for a,state,frozen in original_lights:
   if a.is_alive:a.set_state(state);a.freeze(frozen)
  world.set_weather(original_weather);ego.set_transform(original_pose);ego.set_target_velocity(carla.Vector3D());ego.apply_control(original_control)
  subprocess.run(['ssh','-o','ControlPath=none','rp@192.168.99.21','systemctl --user start codrive-pi'],check=False)
  if g29_active:
   subprocess.run(['systemd-run','--user','--unit=codrive-carla-g29','--collect','-p','Restart=on-failure','-p','RestartSec=2','-p','StandardOutput=append:/tmp/codrive-carla-g29.log','-p','StandardError=append:/tmp/codrive-carla-g29.log','/bin/bash',str(ROOT/'scripts/run-carla-g29.sh')],check=False)
  if scenario_active:
   subprocess.run(['systemd-run','--user','--unit=codrive-carla-scenarios','--collect','-p','WorkingDirectory='+str(ROOT),'-p','StandardOutput=append:/tmp/codrive-carla-scenarios.log','-p','StandardError=append:/tmp/codrive-carla-scenarios.log','/bin/bash',str(ROOT/'scripts/run-carla-scenario.sh'),'--config','config/carla/scenarios/'+current_scene+'.json','--no-g29','--no-pi-recorder'],check=False)
 (out/'session.json').write_text(json.dumps({**metadata,'frames':len(records),'status':'captured'},indent=2)+'\n')
 # Copy only captured IDs; registration and other sessions are not included in the dataset.
 manifest='\n'.join(r['id']+ext for r in records for ext in ('.png','.json'))+'\n'
 remote_manifest=REMOTE+'/export-list.txt';ssh('from pathlib import Path;Path('+repr(remote_manifest)+').write_text('+repr(manifest)+')')
 address,control=ADDRESS,CONTROL
 data=subprocess.check_output(['ssh','-o','ControlPath='+control,address,'tar -cf - -C '+REMOTE+' -T '+remote_manifest],timeout=120)
 (out/'captures.tar').write_bytes(data);subprocess.run(['tar','-xf',str(out/'captures.tar'),'-C',str(out)],check=True)
 print('DATASET',out,'frames',len(records),flush=True)


if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=Path('build/perception-eval-20261027/dataset-v2'));p.add_argument('--max-frames',type=int,default=0);p.add_argument('--intersections',type=int,nargs='+',default=[11,22]);p.add_argument('--distances',type=int,nargs='+',default=[15,25,40,60,80]);p.add_argument('--weathers',nargs='+',default=['clear_day','rain_night']);main(p.parse_args())
