"""Supplement static map props omitted by CARLA's dynamic actor enumeration.

Replays only a temporary semantic sensor pose. No ego/actor movement or truth
publication. Visibility is a static-scene replay and excludes dynamic overlaps.
"""
import argparse,json,threading,time
from pathlib import Path
import carla,cv2,numpy as np
from collect_dataset import GroundTruth,TAGS
from annotation_registration import registration,box_to_camera

def main(a):
 root=a.dataset;original=root/'annotations.jsonl';backup=root/'annotations-dynamic-only.jsonl'
 if not backup.exists():backup.write_bytes(original.read_bytes())
 records=[json.loads(s) for s in backup.read_text().splitlines()];base=json.loads((root.parent/'monitor-registration.json').read_text())
 c=carla.Client('localhost',2000);c.set_timeout(5);world=c.get_world();library=world.get_blueprint_library();bp=library.find('sensor.camera.semantic_segmentation')
 for k,v in {'role_name':'codrive_static_truth_audit','image_size_x':'1280','image_size_y':'720','fov':'90','sensor_tick':'.05'}.items():bp.set_attribute(k,v)
 sensor=world.spawn_actor(bp,carla.Transform(carla.Location(z=1000)));truth=GroundTruth(world,sensor,base);sensor.listen(truth.receive);static=[]
 for kind,name in [('car','Car'),('truck','Truck'),('bus','Bus'),('pedestrian','Pedestrians'),('bicycle','Bicycle'),('motorcycle','Motorcycle')]:
  for obj in world.get_environment_objects(getattr(carla.CityObjectLabel,name)):static.append((kind,obj))
 cache={};out=[]
 try:
  for index,r in enumerate(records):
   if r['case'] not in cache:
    t=r['gt']['camera_transform'];sensor.set_transform(carla.Transform(carla.Location(t['x'],t['y'],t['z']),carla.Rotation(pitch=t['pitch'],yaw=t['yaw'],roll=t['roll'])))
    time.sleep(.35)
    with truth.lock:frame,stamp,mask=truth.semantic
    labels=[]
    for kind,obj in static:
     b=truth.project(obj.bounding_box.get_world_vertices(carla.Transform()))
     if b is None:continue
     x1,y1,x2,y2=b;ys,xs=np.where(np.isin(mask[y1:y2+1,x1:x2+1],TAGS[kind]))
     if len(xs)<2:continue
     tight=[x1+int(xs.min()),y1+int(ys.min()),x1+int(xs.max())+1,y1+int(ys.max())+1]
     labels.append({'class':kind,'id':'static:'+str(obj.id),'virtual_bbox':tight,'distance_m':obj.bounding_box.location.distance(sensor.get_location()),'state':None,'primary_signal':False,'visible_semantic_pixels':len(xs),'static_map_prop':True})
    cache[r['case']]=labels
   image=cv2.imread(str(root/r['image']))
   try:local=registration(image,base)
   except ValueError:out.append(r);continue
   new=[]
   for g in cache[r['case']]:
    b=box_to_camera(g['virtual_bbox'],base,local)
    if b[2]-b[0]<2 or b[3]-b[1]<4:continue
    # Conservative: a removed evaluation actor may have occluded this prop.
    if any(min(b[2],d['bbox'][2])>max(b[0],d['bbox'][0]) and min(b[3],d['bbox'][3])>max(b[1],d['bbox'][1]) for d in r['gt']['labels'] if d['class']!='traffic_light'):continue
    new.append({**g,'bbox':b})
   out.append({**r,'gt':{**r['gt'],'labels':r['gt']['labels']+new,'static_visibility_source':'independent semantic sensor static-scene replay; overlapping dynamic regions excluded'}})
   if index%30==0:print(index,len(records),flush=True)
 finally:sensor.stop();sensor.destroy()
 original.write_text('\n'.join(json.dumps(r) for r in out)+'\n');print('DONE',len(out),'props',len(static))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('dataset',type=Path);main(p.parse_args())
