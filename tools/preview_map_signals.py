"""Show four map signal approaches for 10 seconds, then restore the original demo."""
import json,math,os,signal,subprocess,time,threading,urllib.request
from pathlib import Path
import carla,numpy as np,pygame
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'build/map-signal-preview';OUT.mkdir(parents=True,exist_ok=True)
client=carla.Client('127.0.0.1',2000);client.set_timeout(90);world=client.get_world();original_map=world.get_map().name
settings=world.get_settings();weather=world.get_weather();actors=[]
def tj(t):return {'x':t.location.x,'y':t.location.y,'z':t.location.z,'pitch':t.rotation.pitch,'yaw':t.rotation.yaw,'roll':t.rotation.roll}
def transform(t):return carla.Transform(carla.Location(t['x'],t['y'],t['z']),carla.Rotation(pitch=t['pitch'],yaw=t['yaw'],roll=t['roll']))
for a in world.get_actors():
 if not a.type_id.startswith(('vehicle.','sensor.')):continue
 t=a.get_transform();parent=a.parent
 if parent:
  m=np.asarray(parent.get_transform().get_inverse_matrix())@np.asarray(t.get_matrix())
  t=carla.Transform(carla.Location(*map(float,m[:3,3])),carla.Rotation(pitch=math.degrees(math.atan2(m[2,0],math.hypot(m[0,0],m[1,0]))),yaw=math.degrees(math.atan2(m[1,0],m[0,0])),roll=math.degrees(math.atan2(-m[2,1],m[2,2]))))
 actors.append({'id':a.id,'type':a.type_id,'parent':parent.id if parent else None,'attrs':a.attributes,'transform':tj(t)})
hero=next(a for a in world.get_actors().filter('vehicle.*') if a.attributes.get('role_name')=='hero')
if hero.get_velocity().length()>.5 or hero.get_control().throttle>.03:raise RuntimeError('Stop and release accelerator first')
try:scene=json.load(urllib.request.urlopen('http://127.0.0.1:18081/api/state',timeout=2))['scenario_id']
except Exception:scene='pedestrian-lane'
(OUT/'restore.json').write_text(json.dumps({'map':original_map,'actors':actors,'scenario':scene},indent=2))
services=['codrive-carla-scenarios','codrive-carla-g29','codrive-carla-front-screen','codrive-carla-can-sender']
active={s:subprocess.run(['systemctl','--user','is-active','--quiet',s]).returncode==0 for s in services}
bridge_pids=[]
for p in Path('/proc').iterdir():
 if p.name.isdigit():
  try:
   if b'-m\0carla_ros_bridge.bridge\0' in (p/'cmdline').read_bytes():bridge_pids.append(int(p.name))
  except (OSError,PermissionError):pass
sensor=None;screen=None;changed=False

def run_unit(name,args,ros=False):
 cmd=['systemd-run','--user','--unit='+name,'--collect','-p','WorkingDirectory='+str(ROOT),'-p','StandardOutput=append:/tmp/'+name+'.log','-p','StandardError=append:/tmp/'+name+'.log','--setenv=DISPLAY=:1','--setenv=XAUTHORITY=/run/user/1000/gdm/Xauthority']
 subprocess.run(cmd+args,check=True)
def blueprint(w,r):
 b=w.get_blueprint_library().find(r['type'])
 for k,v in r['attrs'].items():
  if b.has_attribute(k) and b.get_attribute(k).is_modifiable:b.set_attribute(k,v)
 return b

def viewpoint(w):
 candidates=[]
 for light in w.get_actors().filter('traffic.traffic_light'):
  for stop in light.get_stop_waypoints():
   boxes=[b for b in light.get_light_boxes() if b.extent.z>.3]
   head=max(boxes,key=lambda b:b.location.z).location if boxes else light.get_location()+carla.Location(z=3.)
   f=stop.transform.get_forward_vector();ahead=(head.x-stop.transform.location.x)*f.x+(head.y-stop.transform.location.y)*f.y
   d=20-ahead;choices=stop.previous(d) if d>0 else stop.next(-d)
   if not choices:continue
   wp=min(choices,key=lambda p:abs((p.transform.rotation.yaw-stop.transform.rotation.yaw+180)%360-180))
   if abs((wp.transform.rotation.yaw-stop.transform.rotation.yaw+180)%360-180)>8:continue
   t=wp.transform;t.location.z+=1.6
   lateral=abs((head.x-t.location.x)*(-f.y)+(head.y-t.location.y)*f.x)
   candidates.append((lateral,light,t))
 if not candidates:raise RuntimeError('No signal approach found')
 return min(candidates,key=lambda r:r[0])[1:]

try:
 for s in services:
  if active[s]:subprocess.run(['systemctl','--user','stop',s],check=True)
 for pid in bridge_pids:
  try:os.kill(pid,signal.SIGINT)
  except ProcessLookupError:pass
 time.sleep(1)
 pygame.display.init();pygame.font.init();screen=pygame.display.set_mode((1920,1080),pygame.NOFRAME,display=1);font=pygame.font.Font(None,54)
 for name in ('Town01','Town02','Town03','Town05'):
  screen.fill((15,15,15));screen.blit(font.render('Loading '+name+' ...',True,(255,255,255)),(60,80));pygame.display.flip();pygame.event.pump()
  print('LOADING',name,flush=True);changed=True;world=client.load_world(name);world.set_weather(carla.WeatherParameters.ClearNoon)
  light,t=viewpoint(world)
  for l in world.get_actors().filter('traffic.traffic_light'):l.freeze(True);l.set_state(carla.TrafficLightState.Red)
  b=world.get_blueprint_library().find('sensor.camera.rgb')
  hd=next(r for r in actors if r['attrs'].get('role_name')=='rgb_front_hd')
  for k,v in hd['attrs'].items():
   if b.has_attribute(k) and b.get_attribute(k).is_modifiable:b.set_attribute(k,v)
  b.set_attribute('role_name','codrive_map_comparison');sensor=world.spawn_actor(b,t);latest=[None];ready=threading.Event()
  def receive(image):latest[0]=np.frombuffer(image.raw_data,np.uint8).reshape(image.height,image.width,4)[:,:,:3][:,:,::-1].copy();ready.set()
  sensor.listen(receive)
  deadline=time.monotonic()+20
  while not ready.is_set() and time.monotonic()<deadline:pygame.event.pump();time.sleep(.05)
  if not ready.is_set():raise RuntimeError('Camera did not start')
  started=time.monotonic();clock=pygame.time.Clock();print('SHOWING',name,'10 seconds',flush=True)
  while time.monotonic()-started<10:
   for event in pygame.event.get():
    if event.type==pygame.QUIT or (event.type==pygame.KEYDOWN and event.key==pygame.K_ESCAPE):raise KeyboardInterrupt()
   image=latest[0]
   if image is not None:screen.blit(pygame.surfarray.make_surface(image.swapaxes(0,1)),(0,0))
   pygame.draw.rect(screen,(15,15,15),(0,0,1920,85));screen.blit(font.render(f'{name} | FOV 90 | 1920x1080 | 20m road approach | {max(0,10-(time.monotonic()-started)):.0f}s',True,(255,255,255)),(25,20));pygame.display.flip();clock.tick(30)
  sensor.stop();sensor.destroy();sensor=None;print('FINISHED',name,flush=True)
finally:
 if sensor:
  try:sensor.stop();sensor.destroy()
  except RuntimeError:pass
 if screen:screen.fill((15,15,15));screen.blit(font.render('Restoring Town10 demo ...',True,(255,255,255)),(60,80));pygame.display.flip()
 print('RESTORING',original_map,flush=True)
 if changed:world=client.load_world(original_map)
 world.apply_settings(settings);world.set_weather(weather)
 if changed:
  remap={}
  for r in sorted(actors,key=lambda a:a['parent'] is not None):
   b=blueprint(world,r);t=transform(r['transform'])
   if r['parent'] is None:t.location.z+=.2
   a=world.spawn_actor(b,t,attach_to=remap.get(r['parent']));remap[r['id']]=a
   if r['type'].startswith('vehicle.'):a.apply_control(carla.VehicleControl(brake=1,hand_brake=True))
 pygame.quit()
 if bridge_pids:run_unit('codrive-carla-ros-bridge',['/bin/bash','/home/khj/2026ws/carla/start-ros-bridge.sh'])
 if active['codrive-carla-front-screen']:run_unit('codrive-carla-front-screen',['/bin/bash','-c','source /home/khj/2026ws/carla/ros2-env.sh; exec /home/khj/2026ws/carla/.venv/bin/python '+str(ROOT/'tools/carla_front_screen.py')+' --carla-direct --display 1 --size 1920x1080 --position 0,0 --borderless'])
 if active['codrive-carla-scenarios']:run_unit('codrive-carla-scenarios',['/bin/bash',str(ROOT/'scripts/run-carla-scenario.sh'),'--config','config/carla/scenarios/'+scene+'.json','--no-g29','--no-pi-recorder'])
 if active['codrive-carla-g29']:run_unit('codrive-carla-g29',['/bin/bash',str(ROOT/'scripts/run-carla-g29.sh')])
 if active['codrive-carla-can-sender']:run_unit('codrive-carla-can-sender',['/bin/bash','/home/khj/2026ws/ESW2026-front-demo/scripts/run-carla-can-sender.sh'])
 print('RESTORED',flush=True)
