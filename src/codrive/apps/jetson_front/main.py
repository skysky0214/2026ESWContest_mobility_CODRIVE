"""Camera preview + CODRIVE inference. Processing time is NOT sensor-to-screen latency."""
import argparse
from dataclasses import asdict
import json
import os
import time
from pathlib import Path
import cv2
import numpy as np
import torch
from codrive.sensor_input.external.front_perception import UltralyticsFrontPerception, YoloFrontPublisher
from codrive.transport import CycloneDdsBus, InMemoryBus
from codrive.contracts.models import to_json
from codrive.hardware.camera import UsbCameraConfig, GmslCameraConfig, camera_source
from codrive.hardware.camera.jetson_video import JetsonVideoCapture
from codrive.hardware.camera.rectify import CameraRectifier
from codrive.sensor_input.external.lane.geometric import GeometricLaneTracker, draw_lane_segments
from codrive import latency_probe as latency
from codrive.transport.mjpeg import MjpegStreamConfig, MjpegStreamServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=['usb', 'argus', 'v4l2', 'test'], default='usb')
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument('--video', type=Path, help='Replay a video file instead of camera input')
    inputs.add_argument('--stream-url', help='CARLA simulation relay HTTP(S) /frame.jpg URL')
    parser.add_argument('--loop', action='store_true', help='Repeat video at EOF')
    parser.add_argument('--device-index', type=int, default=0)
    parser.add_argument('--sensor-id', type=int, default=0)
    parser.add_argument('--width', type=int, default=1280)
    parser.add_argument('--height', type=int, default=720)
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--input-format', choices=['MJPG','YUY2'], default='MJPG')
    parser.add_argument('--pixel-format', default='UYVY')
    parser.add_argument('--perception-profile', type=Path, help='Opt-in calibrated pixel ROI and vertical signal layout')
    parser.add_argument('--model', type=Path, default=Path('artifacts/models/vision/yolo11n.engine'))
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--preview-width', type=int, default=1280,
                        help='Local BBOX debug window width; does not change inference or HMI video')
    parser.add_argument('--camera-calibration', type=Path,
                        default=os.environ.get('CODRIVE_FRONT_CALIBRATION'),
                        help='Measured lens calibration JSON; rectify before inference and Pi preview')
    parser.add_argument('--camera-zoom', type=float, default=1.0,
                        help='Additional rectified-view focal scale (1 to 3)')
    parser.add_argument('--evaluation-capture-dir', type=Path, help='Opt-in lossless request-driven evaluation snapshots')
    parser.add_argument('--max-frames', type=int, default=0)
    parser.add_argument('--log', type=Path, default=None)
    parser.add_argument('--lane-method', choices=['colour','ufld'], default='colour')
    parser.add_argument('--lane-model', type=Path, help='Optional UFLD TensorRT engine path')
    parser.add_argument('--lane-interval', type=int, default=2)
    parser.add_argument('--domain-id', type=int, default=42)
    parser.add_argument('--dry-run', action='store_true', help='Use an in-process bus instead of DDS')
    parser.add_argument('--hmi-stream-host', default='0.0.0.0')
    parser.add_argument('--hmi-stream-port', type=int, default=8080)
    parser.add_argument('--hmi-stream-fps', type=float, default=12.0)
    parser.add_argument('--hmi-stream-width', type=int, default=960)
    parser.add_argument('--hmi-stream-quality', type=int, default=70)
    parser.add_argument('--no-hmi-stream', action='store_true')
    parser.add_argument('--debug-stream-port', type=int, default=0,
                        help='Optional annotated BBOX MJPEG port; 0 disables it')
    parser.add_argument("--ground-calibration", type=Path, default=os.environ.get("CODRIVE_GROUND_CALIBRATION"))
    args = parser.parse_args()
    latency.install_front()
    if not 0 <= args.debug_stream_port <= 65535:
        parser.error('--debug-stream-port must be 0..65535')
    if args.debug_stream_port and not args.no_hmi_stream and args.debug_stream_port == args.hmi_stream_port:
        parser.error('Debug and clean HMI streams require different ports')
    if args.stream_url and args.camera_calibration:
        parser.error('Simulation RGB must not use physical lens calibration; unset CODRIVE_FRONT_CALIBRATION')
    if args.stream_url and args.loop:
        parser.error('--loop applies only to video files')
    if args.lane_interval < 1:
        parser.error('--lane-interval must be positive')
    if args.max_frames < 0:
        parser.error('--max-frames must be nonnegative')
    if args.preview_width < 160:
        parser.error('--preview-width must be at least 160')
    if not 1 <= args.camera_zoom <= 3:
        parser.error('--camera-zoom must be between 1 and 3')
    if args.camera_zoom != 1 and not args.camera_calibration:
        parser.error('--camera-zoom requires --camera-calibration')
    if args.video and not args.video.is_file():
        parser.error(f'Video not found: {args.video}')
    if not args.video and not args.stream_url and args.backend in ('usb','v4l2') and not Path(f'/dev/video{args.device_index}').exists():
        parser.error(f'/dev/video{args.device_index} 없음. 카메라 연결 후 v4l2-ctl --list-devices 로 확인하세요.')
    if args.video or args.stream_url:
        pipeline = None
    elif args.backend == 'test':
        pipeline = ('videotestsrc is-live=true pattern=ball ! '
                    'video/x-raw,width=1280,height=720,framerate=30/1 ! videoconvert ! '
                    'video/x-raw,format=BGR ! appsink name=frames drop=true max-buffers=1 sync=false')
    elif args.backend == 'usb':
        pipeline = camera_source(UsbCameraConfig(device_index=args.device_index,width=args.width,
            height=args.height,fps=args.fps,input_format=args.input_format))
    else:
        pipeline = camera_source(GmslCameraConfig(backend=args.backend, sensor_id=args.sensor_id,
            device=f'/dev/video{args.device_index}',width=args.width,height=args.height,
            fps=args.fps,pixel_format=args.pixel_format))
    if not args.video and not args.stream_url and args.backend in ('v4l2', 'argus'):
        pipeline = pipeline.replace('video/x-raw,format=(string)BGRx',
            'video/x-raw,width=(int)1280,height=(int)720,format=(string)BGRx')
    if pipeline:
        pipeline = pipeline.replace('appsink drop=', 'appsink name=frames drop=')
    rectifier = CameraRectifier(args.camera_calibration, zoom=args.camera_zoom) if args.camera_calibration else None
    if rectifier:
        print(f'Measured lens correction: {rectifier.model}, zoom={args.camera_zoom}, '
              f'valid_pixels={rectifier.valid_fraction:.1%}, file={args.camera_calibration}', flush=True)
    refinement = None
    if args.perception_profile:
        from codrive.sensor_input.external.signal_refinement import SignalRefinement
        refinement = SignalRefinement.load(args.perception_profile)
    perception = UltralyticsFrontPerception(args.model,imgsz=(384,640),device=0,refinement=refinement,estimate_image_lane=not bool(args.ground_calibration))
    if args.stream_url:
        perception.model_version += ':carla_rgb_relay'
    print('Warming up TensorRT...',flush=True)
    for _ in range(5):
        perception.detections(np.zeros((720,1280,3),dtype=np.uint8))
    torch.cuda.synchronize()
    geometry_tracker = GeometricLaneTracker()
    lane_model = None
    if args.lane_method == 'ufld':
        from codrive.sensor_input.external.lane.ufld import UfldLaneDetector
        lane_model = UfldLaneDetector(args.lane_model)
        lane_model.detect(np.zeros((720,1280,3),dtype=np.uint8))
    lane_segments = []
    lane_updated = 0.0
    def open_capture():
        if args.stream_url:
            from codrive.hardware.camera.http_frame import HttpFrameCapture
            return HttpFrameCapture(args.stream_url)
        return JetsonVideoCapture(str(args.video)) if args.video else JetsonVideoCapture(pipeline=pipeline)
    capture = open_capture()
    stream = None
    hmi_stream = None
    debug_stream = None
    preview_enabled = not args.headless
    preview_created = False
    preview_name = 'CODRIVE Front Debug'
    try:
        if args.debug_stream_port:
            debug_stream = MjpegStreamServer(MjpegStreamConfig(
                host=args.hmi_stream_host, port=args.debug_stream_port,
                width=1280, fps=8, jpeg_quality=85))
            debug_stream.start()
            print(f'Annotated BBOX stream: :{debug_stream.port}/stream.mjpg', flush=True)
        if not args.no_hmi_stream:
            hmi_stream = MjpegStreamServer(MjpegStreamConfig(
                host=args.hmi_stream_host,
                port=args.hmi_stream_port,
                width=args.hmi_stream_width,
                fps=args.hmi_stream_fps,
                jpeg_quality=args.hmi_stream_quality,
            ))
            hmi_stream.start()
            print(
                f'HMI camera stream: http://<jetson-front-ip>:{hmi_stream.port}/stream.mjpg',
                flush=True,
            )
        if args.log:
            args.log.parent.mkdir(parents=True,exist_ok=True)
            stream = args.log.open('w')
        bus = InMemoryBus() if args.dry_run else CycloneDdsBus(domain_id=args.domain_id)
        with bus:
            pub = YoloFrontPublisher(bus=bus,perception=perception)
            if args.ground_calibration:
                from codrive.sensor_input.external.ground_geometry import GroundGeometry
                pub.geometry = GroundGeometry.load(args.ground_calibration)
            count = 0
            previous = time.perf_counter()
            print(f'{args.stream_url or args.video or "Camera"} input active. Q / Esc hides debug preview; DDS and HMI continue.',flush=True)
            while not args.max_frames or count < args.max_frames:
                ok,frame = capture.read()
                if not ok:
                    if args.video and args.loop:
                        capture.release()
                        capture = open_capture()
                        geometry_tracker = GeometricLaneTracker()
                        from codrive.sensor_input.external.traffic_signal import TrafficSignalTracker
                        perception.signal_tracker = TrafficSignalTracker(min_detection_confidence=refinement.minimum if refinement else .60)
                        lane_segments = []
                        lane_updated = 0.0
                        continue
                    if args.video:
                        break
                    raise RuntimeError('Camera stream ended')
                received_wall_ns = time.time_ns()
                trace = latency.begin("front_frame")
                capture_wall_ns = capture.capture_time_ns if args.stream_url else received_wall_ns
                if pub.geometry is not None:
                    motion=[]
                    for envelope in bus.take_json("vehicle_state",max_samples=64):
                        try:
                            sample=json.loads(envelope.payload_json);header=sample['header']
                            if (sample.get('can_valid') is True and header['source_id']==envelope.source_id=="pi5.can_ecu"
                                    and abs(header['capture_time_ns']-capture_wall_ns)<=200_000_000):
                                motion.append(sample)
                        except (KeyError,TypeError,ValueError):continue
                    if motion:
                        sample=min(motion,key=lambda m:abs(m['header']['capture_time_ns']-capture_wall_ns))
                        pub.geometry.lane_tracker.update_motion(sample['speed_mps'],sample['steering_wheel_deg'],sample['header']['capture_time_ns'])
                start = time.perf_counter()
                frame = cv2.resize(frame,(1280,720))
                if rectifier:
                    frame = rectifier.apply(frame)
                # Stream timestamps include relay age + conservative request RTT, without remote clock assumptions.
                if lane_model is None:
                    lane_segments = geometry_tracker.detect(frame)
                elif count % args.lane_interval == 0:
                    lane_segments = lane_model.detect(frame)
                    lane_updated = time.perf_counter()
                elif time.perf_counter()-lane_updated > .6:
                    lane_segments = []
                result = pub.publish_frame(frame,capture_time_ns=capture_wall_ns, lane_boundaries=lane_segments)
                torch.cuda.synchronize()
                if args.evaluation_capture_dir:
                    request=args.evaluation_capture_dir/'request.json'
                    if request.exists():
                        import re
                        try:
                            spec=json.loads(request.read_text()); ident=spec['id']
                            if not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',ident):
                                raise ValueError('Invalid capture ID')
                            base=args.evaluation_capture_dir/ident
                            cv2.imwrite(str(base.with_suffix('.png')),frame)
                            raw=[{'label':d.label,'confidence':d.confidence,'bbox':d.bbox_xyxy}
                                 for d in getattr(perception,'last_detections',())]
                            base.with_suffix('.json').write_text(json.dumps({'perception':json.loads(to_json(result)),
                                'raw_detections':raw,'received_ns':received_wall_ns,'capture_spec':spec}))
                            base.with_suffix('.ready').touch()
                        finally:
                            request.unlink(missing_ok=True)
                if hmi_stream is not None:
                    hmi_stream.submit_bgr(frame)
                now = time.perf_counter()
                processing_ms = (now-start)*1000
                if trace is not None:
                    latency.link(frame=latency.header_key(result.header), receipt_ns=received_wall_ns,
                                 input_source='physical_camera' if not args.stream_url and not args.video else 'nonphysical',
                                 lane_fresh=count % args.lane_interval == 0,
                                 source_pts_ns=capture.last_pts_ns)
                    latency.finish(trace)
                fps = 1/(now-previous)
                previous = now
                count += 1
                if stream:
                    stream.write(json.dumps({'frame':count,'source_pts_ns':capture.last_pts_ns,
                        'host_received_ns':received_wall_ns,
                        'timestamp_origin':'relay_ros_receipt_minus_age_and_rtt' if args.stream_url else 'host_receipt_not_sensor_capture',
                        'input_source':'carla_rgb_relay' if args.stream_url else ('video' if args.video else 'physical_camera'),
                        'transport_age_upper_ms':capture.transport_age_upper_ms if args.stream_url else None,
                        'request_roundtrip_ms':capture.request_roundtrip_ms if args.stream_url else None,
                        'relay_frame_age_ms':capture.relay_frame_age_ms if args.stream_url else None,
                        'source_sequence':capture.last_sequence if args.stream_url else None,
                        'lane_method':args.lane_method,'lane_age_ms':(time.perf_counter()-lane_updated)*1000 if lane_model else 0,
                        'lane_boundaries':[asdict(b) for b in lane_segments],
                        'lane_association':pub.geometry.lane_status if pub.geometry is not None else None,'processing_ms':processing_ms,'perception':json.loads(to_json(result))})+'\n')
                if count % 30 == 0:
                    print(f'frame={count} processing={processing_ms:.1f}ms fps={fps:.1f} objects={len(result.objects)}',flush=True)
                if preview_enabled or debug_stream is not None:
                    if preview_enabled and not preview_created:
                        cv2.namedWindow(preview_name, cv2.WINDOW_NORMAL)
                        cv2.resizeWindow(preview_name, args.preview_width, round(args.preview_width * .5625))
                        preview_created = True
                    if lane_model is None:
                        draw_lane_segments(frame, lane_segments)
                    else:
                        from codrive.sensor_input.external.lane.ufld import draw_model_lanes
                        draw_model_lanes(frame, lane_segments, selected_slots=getattr(pub.geometry,"selected_slots",None))
                    cv2.rectangle(frame,(0,0),(1280,105),(20,20,20),-1)
                    label='CARLA RGB STREAM' if args.stream_url else ('VIDEO TEST' if args.video else ('TEST SOURCE' if args.backend=='test' else 'LIVE CAMERA'))
                    label += ' / RECTIFIED' if rectifier else (' / PINHOLE' if args.stream_url else ' / RAW LENS')
                    cv2.putText(frame,f'{label} | {args.lane_method}/{args.lane_interval if lane_model else 1} | frame {count} | {fps:.1f} FPS | {processing_ms:.1f} ms',
                        (15,28),cv2.FONT_HERSHEY_SIMPLEX,.7,(255,255,255),2)
                    lane_state=pub.geometry.lane_status['state'] if pub.geometry is not None else 'untracked'
                    cv2.putText(frame,f'LANE: {lane_state} | candidates: {len(lane_segments)} | objects: {len(result.objects)} | Q/Esc: hide debug',
                        (15,58),cv2.FONT_HERSHEY_SIMPLEX,.55,(255,255,255),1)
                    signal_colour = {'red':(0,0,255),'yellow':(0,255,255),'green':(0,255,0)}.get(result.traffic_light.value,(180,180,180))
                    cv2.putText(frame,f'SIGNAL: {result.traffic_light.value.upper()} | state evidence: {result.traffic_light_confidence:.2f}',
                        (15,88),cv2.FONT_HERSHEY_SIMPLEX,.65,signal_colour,2)
                    # Draw boxes last so the HUD cannot hide detections near the top edge.
                    for obj in result.objects:
                        b=obj.bbox
                        x1,y1,x2,y2=int(b.x1*1280),int(b.y1*720),int(b.x2*1280),int(b.y2*720)
                        cv2.rectangle(frame,(x1,y1),(x2,y2),(0,220,0),2)
                        cv2.putText(frame,f'{obj.class_name} {obj.confidence:.2f}',(x1,max(20,y1-5)),cv2.FONT_HERSHEY_SIMPLEX,.5,(0,220,0),1)
                    selected = perception.signal_tracker.selected_bbox
                    if selected:
                        x1,y1,x2,y2 = selected
                        cv2.rectangle(frame,(int(x1*1280),int(y1*720)),(int(x2*1280),int(y2*720)),(255,200,0),3)
                    if debug_stream is not None:
                        debug_stream.submit_bgr(frame)
                    if preview_enabled:
                        cv2.imshow(preview_name, frame)
                        key = cv2.waitKey(1) & 0xff
                        try:
                            visible = cv2.getWindowProperty(preview_name, cv2.WND_PROP_VISIBLE) >= 1
                        except cv2.error:
                            visible = False
                        if key in (27, ord('q')) or not visible:
                            cv2.destroyAllWindows()
                            preview_enabled = False
                            print('Debug preview hidden; DDS and HMI continue.', flush=True)
            print(f'Completed {count} frames',flush=True)
    finally:
        if debug_stream is not None:
            debug_stream.close()
        if hmi_stream is not None:
            hmi_stream.close()
        capture.release()
        if stream:
            stream.close()
        if not args.headless:
            cv2.destroyAllWindows()

if __name__=='__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
