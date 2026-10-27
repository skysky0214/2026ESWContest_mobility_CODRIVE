import json,math,os,signal,subprocess,time,threading,urllib.request
from pathlib import Path
import carla,numpy as np,pygame
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'build/map-signal-preview'
saved=json.loads((OUT/'restore.json').read_text());original_map=saved['map'];actors=saved['actors'];scene=saved['scenario']
settings=carla.WorldSettings(synchronous_mode=False,no_rendering_mode=False);weather=carla.WeatherParameters.ClearNoon
services=['codrive-carla-scenarios','codrive-carla-g29','codrive-carla-front-screen','codrive-carla-can-sender'];active=dict.fromkeys(services,True)
bridge_pids=[];bridge_was_running=True;sensor=None;screen=None;changed=True;world=None;client=None

def tj(t):return {'x':t.location.x,'y':t.location.y,'z':t.location.z,'pitch':t.rotation.pitch,'yaw':t.rotation.yaw,'roll':t.rotation.roll}

def transform(t):return carla.Transform(carla.Location(t['x'],t['y'],t['z']),carla.Rotation(pitch=t['pitch'],yaw=t['yaw'],roll=t['roll']))

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

def boot_map(name):
    global client
    subprocess.run(['systemctl','--user','stop','codrive-carla-server'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    package=Path('/home/khj/2026ws/carla/CARLA_0.9.16')
    subprocess.run(['systemd-run','--user','--unit=codrive-carla-server','--collect','-p','WorkingDirectory='+str(package),'-p','StandardOutput=append:/home/khj/2026ws/carla/server.log','-p','StandardError=append:/home/khj/2026ws/carla/server.log','--setenv=VK_ICD_FILENAMES=/usr/share/vulkan/icd.d/nvidia_icd.json','--setenv=__NV_PRIME_RENDER_OFFLOAD=1','--setenv=__GLX_VENDOR_LIBRARY_NAME=nvidia',str(package/'CarlaUE4.sh'),'/Game/Carla/Maps/'+name,'-quality-level=Low','-ResX=1280','-ResY=720','-windowed','-RenderOffScreen','-nosound'],check=True)
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        try:
            probe=carla.Client('127.0.0.1',2000);probe.set_timeout(2);result=probe.get_world()
            if result.get_map().name.rsplit('/',1)[-1]==name:client=probe;client.set_timeout(30);return result
        except RuntimeError:pass
        time.sleep(.5)
    raise RuntimeError('CARLA startup timeout: '+name)

try:
 for s in services:
  if active[s]:subprocess.run(['systemctl','--user','stop',s],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 for pid in bridge_pids:
  try:os.kill(pid,signal.SIGINT)
  except ProcessLookupError:pass
 time.sleep(1)
 pygame.display.init();pygame.font.init();screen=pygame.display.set_mode((1920,1080),pygame.NOFRAME,display=1);font=pygame.font.Font(None,54)
 for name in ('Town02','Town03','Town05'):
  screen.fill((15,15,15));screen.blit(font.render('Loading '+name+' ...',True,(255,255,255)),(60,80));pygame.display.flip();pygame.event.pump()
  print('LOADING',name,flush=True);changed=True;world=boot_map(name);world.set_weather(carla.WeatherParameters.ClearNoon)
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
 if changed:world=boot_map(original_map.rsplit('/',1)[-1])
 world.apply_settings(settings);world.set_weather(weather)
 if changed:
  remap={}
  for r in sorted(actors,key=lambda a:a['parent'] is not None):
   b=blueprint(world,r);t=transform(r['transform'])
   if r['parent'] is None:t.location.z+=.2
   a=world.spawn_actor(b,t,attach_to=remap.get(r['parent']));remap[r['id']]=a
   if r['type'].startswith('vehicle.'):a.apply_control(carla.VehicleControl(brake=1,hand_brake=True))
 pygame.quit()
 if bridge_was_running:run_unit('codrive-carla-ros-bridge',['/bin/bash','/home/khj/2026ws/carla/start-ros-bridge.sh'])
 if active['codrive-carla-front-screen']:run_unit('codrive-carla-front-screen',['/bin/bash','-c','source /home/khj/2026ws/carla/ros2-env.sh; exec /home/khj/2026ws/carla/.venv/bin/python '+str(ROOT/'tools/carla_front_screen.py')+' --carla-direct --display 1 --size 1920x1080 --position 0,0 --borderless'])
 if active['codrive-carla-scenarios']:run_unit('codrive-carla-scenarios',['/bin/bash',str(ROOT/'scripts/run-carla-scenario.sh'),'--config','config/carla/scenarios/'+scene+'.json','--no-g29','--no-pi-recorder'])
 if active['codrive-carla-g29']:run_unit('codrive-carla-g29',['/bin/bash',str(ROOT/'scripts/run-carla-g29.sh')])
 if active['codrive-carla-can-sender']:run_unit('codrive-carla-can-sender',['/bin/bash','/home/khj/2026ws/ESW2026-front-demo/scripts/run-carla-can-sender.sh'])
 print('RESTORED',flush=True)
