"""Relay only existing CARLA front RGB pixels to Jetson; no actor-state inputs."""
import argparse
import signal
import time

import cv2
import numpy as np

from codrive.hardware.camera.frame_relay import FrameRelay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--carla-direct',action='store_true',help='Read existing HD RGB camera directly without ROS image serialization')
    parser.add_argument('--topic', default='/carla/hero/rgb_front/image')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=18082)
    parser.add_argument('--fps', type=float, default=20)
    parser.add_argument('--width', type=int, default=1280, help='Maximum transmitted width; never upscale')
    parser.add_argument('--jpeg-quality', type=int, default=95)
    args = parser.parse_args()
    if not 1 <= args.fps <= 30:
        parser.error('fps must be in [1,30]')
    if not 160 <= args.width <= 3840 or not 20 <= args.jpeg_quality <= 100:
        parser.error('width must be 160..3840 and JPEG quality 20..100')
    node=None;camera=None
    if not args.carla_direct:
        import rclpy
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import Image
        rclpy.init()
        node = rclpy.create_node('codrive_carla_camera_relay')
    latest = [None, None, None]
    previous_stamp = None
    stopped = False

    def receive(message):
        nonlocal previous_stamp
        stamp = (message.header.stamp.sec, message.header.stamp.nanosec)
        if stamp != previous_stamp:
            latest[:] = [message, time.monotonic(), stamp]
            previous_stamp = stamp

    def stop(*_):
        nonlocal stopped
        stopped = True

    if args.carla_direct:
        import carla
        from types import SimpleNamespace
        client=carla.Client('127.0.0.1',2000);client.set_timeout(4)
        camera=next(a for a in client.get_world().get_actors().filter('sensor.camera.rgb') if a.attributes.get('role_name')=='rgb_front_hd')
        def direct_receive(image):
            message=SimpleNamespace(data=image.raw_data,width=image.width,height=image.height,step=image.width*4,encoding='bgra8')
            latest[:]=[message,time.monotonic(),image.frame]
        camera.listen(direct_receive)
    else:
        qos=qos_profile_sensor_data;qos.depth=1
        node.create_subscription(Image,args.topic,receive,qos)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    relay = FrameRelay(args.host, args.port)
    relay.start()
    encoded_stamp = None
    next_frame = 0
    print(f'CARLA RGB relay ready on :{args.port}/frame.jpg', flush=True)
    try:
        while not stopped and (args.carla_direct or rclpy.ok()):
            if node is not None:rclpy.spin_once(node, timeout_sec=.005)
            else:time.sleep(.002)
            message, received, stamp = latest
            now = time.monotonic()
            if message is None or stamp == encoded_stamp or now < next_frame:
                continue
            channels = {'bgra8': 4, 'rgba8': 4, 'bgr8': 3, 'rgb8': 3}.get(message.encoding)
            if channels is None:
                raise ValueError(f'Unsupported RGB encoding: {message.encoding}')
            array = np.frombuffer(message.data, np.uint8).reshape(message.height, message.step)
            bgr = array[:, :message.width*channels].reshape(message.height, message.width, channels)[:, :, :3]
            if message.encoding.startswith('rgb'):
                bgr = bgr[:, :, ::-1]
            if message.width > args.width:
                bgr = cv2.resize(bgr, (args.width, round(message.height*args.width/message.width)), interpolation=cv2.INTER_AREA)
            ok, data = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, args.jpeg_quality])
            if ok:
                relay.publish(data.tobytes(), received)
                encoded_stamp = stamp
                next_frame = now+1/args.fps
    finally:
        relay.close()
        if camera is not None:camera.stop()
        if node is not None:
            node.destroy_node()
            rclpy.shutdown()


if __name__ == '__main__':
    main()
