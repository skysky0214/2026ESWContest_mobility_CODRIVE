"""Real-camera validation of a controlled CARLA target; never injects DDS/risk inputs."""
import json
import math
from pathlib import Path
import time
import urllib.request
import carla

client=carla.Client('127.0.0.1',2000);client.set_timeout(4);world=client.get_world()
ego=next(a for a in world.get_actors().filter('vehicle.*') if a.attributes.get('role_name')=='hero')
if ego.get_velocity().length()>=.5 or ego.get_control().throttle>.03:raise SystemExit('Stop/release ego first')
role='codrive_screen_calibration_target'
for old in world.get_actors().filter('vehicle.*'):
    if old.attributes.get('role_name')==role:old.destroy()
base=ego.get_transform();location=base.transform(carla.Location(x=14,z=.18))
bp=world.get_blueprint_library().find('vehicle.audi.a2');bp.set_attribute('role_name',role)
target=world.try_spawn_actor(bp,carla.Transform(location,carla.Rotation(yaw=base.rotation.yaw+180)))
if target is None:raise SystemExit('Target spawn blocked')
target.apply_control(carla.VehicleControl(brake=1))
rows=[]

def observe(phase):
    with urllib.request.urlopen('http://192.168.99.21:8080/api/state',timeout=2) as response:s=json.load(response)
    p=target.get_location();e=ego.get_transform();f=e.get_forward_vector()
    forward=(p.x-e.location.x)*f.x+(p.y-e.location.y)*f.y
    rows.append({'time_ns':time.time_ns(),'phase':phase,'target_center_forward_m':forward,
                 'target_near_surface_reference_m':forward-target.bounding_box.extent.x,
                 'snapshot_external':(s.get('snapshot') or {}).get('external'),
                 'corridor':s.get('driving_corridor'),'assessment':s.get('risk_assessment'),
                 'command':s.get('command'),'speech':s.get('speech')})
    return forward

try:
    for _ in range(10):observe('spawn_settling');time.sleep(.2)
    for _ in range(15):observe('stationary_metric_reference');time.sleep(.2)
    print('Target',target.id,'baseline recorded; starting controlled approach',flush=True)
    target.apply_control(carla.VehicleControl(brake=0))
    forward=base.get_forward_vector();target.enable_constant_velocity(carla.Vector3D(x=2.5,y=0))
    start=time.monotonic()
    while time.monotonic()-start<3:
        if ego.get_velocity().length()>.5:break
        if observe('approach')<=9:break
        time.sleep(.1)
finally:
    if target.is_alive:
        target.disable_constant_velocity();target.set_target_velocity(carla.Vector3D())
        target.apply_control(carla.VehicleControl(brake=1))
for _ in range(20):observe('stopped_after_approach');time.sleep(.2)
out=Path('build/screen-warning-validation-20261027-stable.json');out.parent.mkdir(exist_ok=True)
out.write_text(json.dumps({'target_id':target.id,'method':'Actual CARLA pixels filmed by physical OX08BC camera; actor coordinates recorded only as reference','rows':rows},ensure_ascii=False,indent=2)+'\n')
commands=[r['command'] for r in rows if r.get('command')]
print('Commands',[(c['risk_code'],c['display_text']) for c in commands],flush=True)
print('Saved',out,flush=True)
