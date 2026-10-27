"""Check ordered hardware decode against software frames and compare detections."""
import ast, json, re
from pathlib import Path
import cv2, numpy as np
from codrive.hardware.camera.jetson_video import JetsonVideoCapture
out=Path('test-results')
a=cv2.VideoCapture('test_driving.mp4'); b=JetsonVideoCapture('test_driving.mp4')
errors=[]
try:
 for i in range(300):
  oka,fa=a.read();okb,fb=b.read();assert oka and okb, i
  fa=cv2.resize(fa,(1280,720))
  errors.append(float(np.abs(fa.astype(np.float32)-fb.astype(np.float32)).mean()))
finally:
 a.release();b.release()
x=json.loads((out/'benchmark-trt-fp16.json').read_text())['detections']
y=json.loads((out/'benchmark-trt-hwdecode.json').read_text())['detections']
def box(d):return [float(v) for v in re.findall(r'=([-+0-9.eE]+)',d['bbox'])]
def iou(a,b):
 ax,ay,az,aw=box(a);bx,by,bz,bw=box(b)
 inter=max(0,min(az,bz)-max(ax,bx))*max(0,min(aw,bw)-max(ay,by))
 union=(az-ax)*(aw-ay)+(bz-bx)*(bw-by)-inter
 return inter/union if union else 0
matched=0; overlaps=[]
for old,new in zip(x,y):
 available=set(range(len(new)))
 for d in sorted(old,key=lambda d:-d['confidence']):
  candidates=[(iou(d,new[j]),j) for j in available if new[j]['class']==d['class']]
  if not candidates:continue
  score,j=max(candidates)
  if score>=.5:
   matched+=1;overlaps.append(score);available.remove(j)
result={'frames':300,'same_index_pixel_mae_0_255_mean':float(np.mean(errors)),
        'same_index_pixel_mae_p95':float(np.percentile(errors,95)),
        'software_detections':sum(map(len,x)),'hardware_detections':sum(map(len,y)),
        'same_class_iou_05_matches':matched,'matched_mean_iou':float(np.mean(overlaps))}
(out/'decode-comparison.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
