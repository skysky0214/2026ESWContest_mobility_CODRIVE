"""Replay the complete production detector on evaluation PNGs, optionally at fixed proposal cadence."""
import argparse,json
from pathlib import Path
import cv2
from codrive.sensor_input.external.front_perception import UltralyticsFrontPerception
from codrive.sensor_input.external.signal_refinement import SignalRefinement
from codrive.contracts.models import Header
p=argparse.ArgumentParser();p.add_argument('--profile',required=True);p.add_argument('--annotations',type=Path,required=True);p.add_argument('--images',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--proposal-step-s',type=float,default=.1);a=p.parse_args()
ref=SignalRefinement.load(a.profile);clock=[0.];ref.clock=lambda:clock[0]
model=UltralyticsFrontPerception('artifacts/models/vision/yolo11n.engine',imgsz=(384,640),device=0,refinement=ref,estimate_image_lane=False);out={}
for i,line in enumerate(a.annotations.read_text().splitlines()):
 r=json.loads(line);im=cv2.imread(str(a.images/r['image']));clock[0]=i*a.proposal_step_s
 result=model.perception_from_frame(im,header=Header.now('offline-replay',i),camera_id='front')
 out[r['id']]={'detections':[{'label':o.class_name,'confidence':o.confidence,'bbox':[o.bbox.x1*1280,o.bbox.y1*720,o.bbox.x2*1280,o.bbox.y2*720]} for o in result.objects],'traffic_light':result.traffic_light.value,'traffic_light_evidence':result.traffic_light_confidence}
a.output.write_text(json.dumps(out));print('REPLAY',len(out))
