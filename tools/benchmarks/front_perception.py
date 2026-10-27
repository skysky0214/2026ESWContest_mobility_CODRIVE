"""Sequential video benchmark; timings include GPU synchronization, exclude warmup."""
import argparse, json, time
from pathlib import Path
from collections import Counter
import cv2, numpy as np, torch
from ultralytics import YOLO
from codrive.sensor_input.external.front_perception import UltralyticsFrontPerception, YoloFrontPublisher
from codrive.transport import InMemoryBus

p=argparse.ArgumentParser()
p.add_argument('--model',default='yolo11n.pt')
p.add_argument('--frames',type=int,default=300)
p.add_argument('--output',required=True)
p.add_argument('--decoder', choices=['opencv', 'jetson'], default='opencv')
p.add_argument('--cv-threads', type=int, default=6)
p.add_argument('--lane-method', choices=['none','colour','ufld'], default='none')
p.add_argument('--lane-interval', type=int, default=1)
a=p.parse_args()
cv2.setNumThreads(a.cv_threads)
if a.decoder == 'jetson':
 from codrive.hardware.camera.jetson_video import JetsonVideoCapture
 capture = lambda: JetsonVideoCapture('test_driving.mp4')
else:
 capture = lambda: cv2.VideoCapture('test_driving.mp4')
cap=capture()
assert cap.isOpened()
ok, first=cap.read(); assert ok
first=cv2.resize(first,(1280,720))
# Match 384x640 network input for both backends on 16:9 video.
perception=UltralyticsFrontPerception(a.model,model=YOLO(a.model,task='detect'),imgsz=(384,640),device=0)
assert a.lane_interval > 0
from codrive.sensor_input.external.lane.geometric import GeometricLaneTracker
geometry_tracker=GeometricLaneTracker()
lane_model=None
if a.lane_method=='ufld':
 from codrive.sensor_input.external.lane.ufld import UfldLaneDetector
 lane_model=UfldLaneDetector()
 lane_model.detect(first)
rows=[]; counts=Counter(); detections=[]
lane_records=[]; lane_times=[]; lanes=[]
with InMemoryBus() as bus:
 pub=YoloFrontPublisher(bus=bus,perception=perception)
 t=time.perf_counter()
 for _ in range(10): pub.publish_frame(first); torch.cuda.synchronize()
 warmup=time.perf_counter()-t
 cap.release(); cap=capture()
 for i in range(a.frames):
  started=time.perf_counter()
  ok,frame=cap.read()
  if not ok: break
  frame=cv2.resize(frame,(1280,720)); decoded=time.perf_counter()
  result=pub.publish_frame(frame)
  lane_started=time.perf_counter()
  if a.lane_method=='colour':
   lanes=geometry_tracker.detect(frame)
  elif lane_model and i%a.lane_interval==0:
   lanes=lane_model.detect(frame)
  torch.cuda.synchronize(); finished=time.perf_counter()
  lane_times.append((finished-lane_started)*1000)
  lane_records.append({b.side: list(b.points) if hasattr(b,'points') else [b.x_top,b.x_bottom] for b in lanes})
  rows.append({'frame':i,'decode_resize_ms':(decoded-started)*1000,'perception_publish_ms':(finished-decoded)*1000,'total_ms':(finished-started)*1000})
  counts.update(o.class_name for o in result.objects)
  detections.append([{'class':o.class_name,'confidence':o.confidence,'bbox':str(o.bbox)} for o in result.objects])
  if (i+1)%50==0: print(f'{i+1}/{a.frames}',flush=True)
cap.release()
assert len(rows)==a.frames
summary={'model':a.model,'decoder':a.decoder,'cv_threads':a.cv_threads,'lane_method':a.lane_method,'lane_interval':a.lane_interval,'lane_step_mean_ms':float(np.mean(lane_times)),'frames':len(rows),'network_shape':[384,640],'warmup_seconds':warmup,'object_counts':dict(counts)}
for key in ['decode_resize_ms','perception_publish_ms','total_ms']:
 v=np.array([r[key] for r in rows]); summary[key]={'mean':float(v.mean()),'p50':float(np.percentile(v,50)),'p95':float(np.percentile(v,95)),'p99':float(np.percentile(v,99)),'max':float(v.max())}
summary['throughput_fps']=1000/summary['total_ms']['mean']
summary['over_33ms_pct']=100*sum(r['total_ms']>1000/30 for r in rows)/len(rows)
summary['over_66ms_pct']=100*sum(r['total_ms']>1000/15 for r in rows)/len(rows)
Path(a.output).write_text(json.dumps({'summary':summary,'frames':rows,'detections':detections,'lanes':lane_records},indent=2))
print(json.dumps(summary,indent=2),flush=True)
