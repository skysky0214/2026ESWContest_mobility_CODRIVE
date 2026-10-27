"""Display the existing CARLA front RGB topic without HUD or vehicle controls."""
import argparse
import json
import os
import signal
import time
import threading
import urllib.request
from pathlib import Path

import numpy as np
import pygame
import rclpy
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evaluation-markers', action='store_true', help='Known corner fiducials for frame-level offline annotation registration')
    parser.add_argument('--carla-direct', action='store_true', help='Local CARLA sensor callback only; no network relay or ROS image subscription')
    parser.add_argument('--camera-fov',type=float,help='Recreate direct HD camera at this FOV, preserving mount and image attributes')
    parser.add_argument('--topic', default='/carla/hero/rgb_front/image')
    parser.add_argument('--display', type=int, default=0)
    parser.add_argument('--fullscreen', action='store_true')
    parser.add_argument('--borderless', action='store_true', help='Monitor-sized window that stays visible when focus changes')
    parser.add_argument('--scenario-url', default='http://127.0.0.1:18081',
                        help='Local saved-scenario controller; R requests reset after stopping')
    parser.add_argument('--position', default='0,0')
    parser.add_argument('--size', default='1280x720')
    parser.add_argument('--status', type=Path, default=Path('/tmp/codrive-front-screen.json'))
    args = parser.parse_args()
    width, height = map(int, args.size.split('x'))
    os.environ['SDL_VIDEO_WINDOW_POS'] = args.position
    os.environ['SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS'] = '0'
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_caption('CODRIVE CARLA Front - R scenario reset / F fullscreen / Esc close')
    window_flags = pygame.NOFRAME if args.borderless else pygame.RESIZABLE
    flags = pygame.FULLSCREEN if args.fullscreen else window_flags
    screen = pygame.display.set_mode((0, 0) if args.fullscreen else (width, height), flags,
                                     display=args.display)
    font = pygame.font.Font(None, 38)
    node = None
    direct_camera = None
    if not args.carla_direct:
        rclpy.init()
        node = rclpy.create_node('codrive_front_screen')
    latest = [None, 0.0]
    counter = 0
    stopped = False

    def receive(message):
        latest[:] = [message, time.monotonic()]

    def stop(*_):
        nonlocal stopped
        stopped = True

    def reset_scenario(ident=None):
        try:
            request = urllib.request.Request(args.scenario_url.rstrip('/') + ('/api/select' if ident else '/api/reset'),
                data=json.dumps({'id': ident} if ident else {}).encode(), headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(request, timeout=2) as response:
                response.read()
        except (OSError, ValueError) as exc:
            print(f'Scenario reset unavailable: {exc}', flush=True)

    scene_state = [{}, 0.]
    scene_stop = threading.Event()
    def poll_scene():
        while not scene_stop.is_set():
            try:
                with urllib.request.urlopen(args.scenario_url.rstrip('/')+'/api/state', timeout=.6) as response:
                    scene_state[:] = [json.load(response), time.monotonic()]
            except (OSError, ValueError):
                pass
            scene_stop.wait(.3)
    scene_thread = threading.Thread(target=poll_scene, daemon=True)
    scene_thread.start()
    choices = {pygame.K_1:'pedestrian-lane', pygame.K_2:'emergency-yellow',
               pygame.K_3:'rain-night-slow-vehicle', pygame.K_4:'rapid-acceleration'}
    qos = qos_profile_sensor_data
    qos.depth = 1
    if args.carla_direct:
        import carla
        from types import SimpleNamespace
        client=carla.Client('127.0.0.1',2000);client.set_timeout(4)
        candidates=[a for a in client.get_world().get_actors().filter('sensor.camera.rgb')
                    if a.attributes.get('role_name')=='rgb_front_hd'
                    and a.parent and a.parent.attributes.get('role_name')=='hero']
        if len(candidates)!=1:raise RuntimeError('Expected one existing hero HD RGB camera')
        direct_camera=candidates[0]
        if args.camera_fov is not None and abs(float(direct_camera.attributes['fov'])-args.camera_fov)>.01:
            if not 20<=args.camera_fov<=120:raise ValueError('Camera FOV must be 20..120 degrees')
            world=client.get_world();parent=direct_camera.parent
            matrix=np.asarray(parent.get_transform().get_inverse_matrix())@np.asarray(direct_camera.get_transform().get_matrix())
            import math
            mount=carla.Transform(carla.Location(*map(float,matrix[:3,3])),carla.Rotation(
                pitch=math.degrees(math.atan2(matrix[2,0],math.hypot(matrix[0,0],matrix[1,0]))),
                yaw=math.degrees(math.atan2(matrix[1,0],matrix[0,0])),roll=math.degrees(math.atan2(-matrix[2,1],matrix[2,2]))))
            blueprint=world.get_blueprint_library().find(direct_camera.type_id)
            for key,value in direct_camera.attributes.items():
                if blueprint.has_attribute(key) and blueprint.get_attribute(key).is_modifiable:blueprint.set_attribute(key,value)
            blueprint.set_attribute('fov',str(args.camera_fov))
            direct_camera.destroy()
            direct_camera=world.spawn_actor(blueprint,mount,attach_to=parent)
            print(f'HD camera FOV set to {args.camera_fov} degrees',flush=True)
        def direct_receive(image):
            receive(SimpleNamespace(data=image.raw_data,width=image.width,height=image.height,
                                    step=image.width*4,encoding='bgra8'))
        direct_camera.listen(direct_receive)
    else:
        node.create_subscription(Image, args.topic, receive, qos)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    clock = pygame.time.Clock()
    last_drawn = None
    surface = None
    next_status = 0.0
    eval_markers=[]
    if args.evaluation_markers:
        import cv2
        dictionary=cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        for ident,(x,y) in zip((40,41,42,43),((160,120),(1500,120),(1500,900),(160,900))):
            tile=np.full((104,104,3),255,np.uint8)
            marker=cv2.aruco.generateImageMarker(dictionary,ident,64)
            tile[20:84,20:84]=cv2.cvtColor(marker,cv2.COLOR_GRAY2RGB)
            eval_markers.append((pygame.surfarray.make_surface(tile.swapaxes(0,1)),(x-20,y-20)))
    try:
        while not stopped and (args.carla_direct or rclpy.ok()):
            for event in pygame.event.get():
                if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                    stopped = True
                if event.type == pygame.KEYDOWN and event.key in (pygame.K_f, pygame.K_F11):
                    args.fullscreen = not args.fullscreen
                    screen = pygame.display.set_mode((0, 0) if args.fullscreen else (width, height),
                        pygame.FULLSCREEN if args.fullscreen else window_flags, display=args.display)
                if event.type == pygame.KEYDOWN and event.key == pygame.K_r:
                    threading.Thread(target=reset_scenario, daemon=True).start()
                if event.type == pygame.KEYDOWN and event.key in choices:
                    threading.Thread(target=reset_scenario, args=(choices[event.key],), daemon=True).start()
            if node is not None:
                rclpy.spin_once(node, timeout_sec=0)
            message, received = latest
            age = time.monotonic() - received
            if message is not None and message is not last_drawn:
                channels = {'bgra8': 4, 'rgba8': 4, 'bgr8': 3, 'rgb8': 3}.get(message.encoding)
                if channels is None:
                    raise ValueError('Unsupported RGB encoding: ' + message.encoding)
                array = np.frombuffer(message.data, dtype=np.uint8).reshape(message.height, message.step)
                rgb = array[:, :message.width * channels].reshape(message.height, message.width, channels)[:, :, :3]
                if message.encoding.startswith('bgr'):
                    rgb = rgb[:, :, ::-1]
                surface = pygame.surfarray.make_surface(rgb.swapaxes(0, 1))
                counter += 1
                last_drawn = message
            screen.fill((0, 0, 0))
            if surface is not None and age < 1.0:
                sw, sh = screen.get_size()
                scale = min(sw / surface.get_width(), sh / surface.get_height())
                size = round(surface.get_width() * scale), round(surface.get_height() * scale)
                screen.blit(pygame.transform.smoothscale(surface, size), ((sw-size[0])//2, (sh-size[1])//2))
            else:
                screen.blit(font.render('Waiting for CARLA front camera', True, (220, 220, 220)), (30, 40))
            state, scene_received = scene_state
            if time.monotonic()-scene_received < 2:
                lines = ['Scenes: 1 Pedestrian/lane | 2 Siren/yellow | 3 Rain/night/lead | 4 Acceleration | R Reset (stopped)']
                if state.get('kind') == 'emergency_yellow':
                    cue = 'PLAY PHONE SIREN NOW - REAR LEFT' if state.get('state') in ('yellow_siren_cue','red_rear_approach') else 'Approach intersection - play phone siren when light turns yellow'
                    lines.append(cue+' | Signal: '+state.get('traffic_light_state','--'))
                elif state.get('kind') == 'rain_night_slow':
                    lines.append('DEMO RAIN / NIGHT - lead vehicle 10 km/h - brake before collision')
                if state.get('state') == 'target_missing':
                    lines.append('Scenario target missing - stop, release accelerator, press R to reset')
                for i, line in enumerate(lines):
                    label = font.render(line, True, (255,225,120) if i else (235,235,235))
                    panel = pygame.Surface((label.get_width()+20,label.get_height()+8));panel.fill((15,22,30))
                    screen.blit(panel,(10,10+i*44));screen.blit(label,(20,14+i*44))
            if eval_markers:
                for tile,position in eval_markers:screen.blit(tile,position)
            pygame.display.flip()
            if time.monotonic() >= next_status:
                args.status.write_text(json.dumps({'ready': surface is not None and age < 1.0,
                    'pid': os.getpid(), 'scenario_control_url': args.scenario_url,
                    'frames': counter, 'frame_age_ms': round(age * 1000, 1) if message else None,
                    'topic': args.topic, 'input':'carla_direct_local' if args.carla_direct else 'ros_image', 'fullscreen': args.fullscreen, 'borderless': args.borderless, 'display': args.display,
                    'camera_fov':float(direct_camera.attributes['fov']) if direct_camera else None, 'source_size': [message.width, message.height] if message else None})+'\n')
                next_status = time.monotonic() + 1
            clock.tick(40)
    finally:
        scene_stop.set()
        scene_thread.join(timeout=1)
        if direct_camera is not None:
            direct_camera.stop()
        if node is not None:
            node.destroy_node()
            rclpy.shutdown()
        pygame.quit()


if __name__ == '__main__':
    main()
