"""Paired inference/postprocess measurement; excludes capture/DDS/HMI/TTS."""
import argparse,json,time
from pathlib import Path
import cv2,numpy as np,torch
from codrive.sensor_input.external.front_perception import UltralyticsFrontPerception
from codrive.sensor_input.external.signal_refinement import SignalRefinement
from codrive.contracts.models import Header
p=argparse.ArgumentParser();p.add_argument('--images',nargs='+',required=True);p.add_argument('--profile',required=True);p.add_argument('--output',required=True);a=p.parse_args()
base=UltralyticsFrontPerception('artifacts/models/vision/yolo11n.engine',imgsz=(384,640),device=0)
refined=UltralyticsFrontPerception('artifacts/models/vision/yolo11n.engine',model=base.model,imgsz=(384,640),device=0,refinement=SignalRefinement.load(a.profile))
optimized=UltralyticsFrontPerception('artifacts/models/vision/yolo11n.engine',model=base.model,imgsz=(384,640),device=0,refinement=SignalRefinement.load(a.profile),estimate_image_lane=False)
images=[cv2.imread(x) for x in a.images];times={'baseline':[],'refined':[],'refined_without_redundant_hough':[]}
for k in range(33):
 for name,model in [('baseline',base),('refined',refined),('refined_without_redundant_hough',optimized)]:
  torch.cuda.synchronize();t=time.perf_counter();model.perception_from_frame(images[k%len(images)],header=Header.now('benchmark',k),camera_id='front');torch.cuda.synchronize();elapsed=time.perf_counter()-t
  if k>=3:times[name].append(elapsed)
result={'scope':'Paired YOLO inference and perception postprocess only; no capture/rectification/UFLD/DDS/HMI/TTS; camera service paused; 3 warmups + 30 samples per variant','seconds':{key:{'mean':float(np.mean(v)),'p50':float(np.median(v)),'p95':float(np.percentile(v,95))} for key,v in times.items()},'samples':times}
Path(a.output).write_text(json.dumps(result,indent=2));print(json.dumps(result['seconds']))
