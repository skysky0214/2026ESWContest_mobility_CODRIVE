"""Replace a stopped demo ego, preserving attached RGB camera attributes/mount."""
import json,time
from pathlib import Path
import carla,numpy as np
ROOT=Path(__file__).resolve().parent.parent
c=carla.Client('127.0.0.1',2000);c.set_timeout(10);w=c.get_world()
ego=next(a for a in w.get_actors().filter('vehicle.*') if a.attributes.get('role_name')=='hero')
if ego.get_velocity().length()>.1 or ego.get_control().throttle>.03:raise RuntimeError('Stop and release accelerator before vehicle replacement')
transform=ego.get_transform();children=[a for a in w.get_actors() if a.parent and a.parent.id==ego.id]
records=[]
for a in children:
 m=np.asarray(transform.get_inverse_matrix())@np.asarray(a.get_transform().get_matrix())
 mount=carla.Transform(carla.Location(x=float(m[0,3]),y=float(m[1,3]),z=float(m[2,3])),carla.Rotation())
 records.append((a.type_id,dict(a.attributes),mount))
snapshot={'old_type':ego.type_id,'old_id':ego.id,'location':[transform.location.x,transform.location.y,transform.location.z],'yaw':transform.rotation.yaw,'sensors':[{'type':typ,'attributes':attrs,'mount':[m.location.x,m.location.y,m.location.z]} for typ,attrs,m in records]}
(ROOT/'build/vehicle-swap/before.json').write_text(json.dumps(snapshot,indent=2))
for a in children:a.destroy()
ego.destroy()
bp=w.get_blueprint_library().find('vehicle.audi.a2');bp.set_attribute('role_name','hero');transform.location.z+=.5;transform.rotation.pitch=0;transform.rotation.roll=0
new=w.try_spawn_actor(bp,transform)
if new is None:
 fallback=w.get_blueprint_library().find(snapshot['old_type']);fallback.set_attribute('role_name','hero');new=w.spawn_actor(fallback,transform)
new.apply_control(carla.VehicleControl(brake=1.,hand_brake=True));time.sleep(1.5)
for typ,attrs,mount in records:
 b=w.get_blueprint_library().find(typ)
 for key,value in attrs.items():
  if b.has_attribute(key) and b.get_attribute(key).is_modifiable:b.set_attribute(key,value)
 w.spawn_actor(b,mount,attach_to=new)
p=new.get_physics_control();bb=new.bounding_box
inverse=np.asarray(new.get_transform().get_inverse_matrix())
local=[inverse@np.array([v.position.x/100,v.position.y/100,v.position.z/100,1]) for v in p.wheels]
front=max(v[0] for v in local);rear=min(v[0] for v in local)
result={'blueprint':new.type_id,'actor_id':new.id,'wheelbase_m':front-rear,'rear_axle_offset_m':-rear,'vehicle_half_width_m':bb.extent.y,'vehicle_front_m':bb.extent.x+bb.location.x,'vehicle_rear_m':bb.extent.x-bb.location.x,'maximum_road_wheel_deg':max(x.max_steer_angle for x in p.wheels),'steering_curve_kmh':[[v.x,v.y] for v in p.steering_curve],'mass_kg':p.mass}
(ROOT/'config/carla/ego-vehicle.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
if new.type_id!='vehicle.audi.a2':raise RuntimeError('Requested vehicle spawn failed; restored original vehicle')
