"""Offline native-resolution traffic-light crop experiment using the deployed engine."""
import argparse,json,time
from pathlib import Path
import cv2
from ultralytics import YOLO

def overlap(a,b):
 inter=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
 return inter/max(1,(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter)
def main(a):
 model=YOLO(a.model);records=[json.loads(s) for s in a.annotations.read_text().splitlines()];out={}
 for k,r in enumerate(records):
  image=cv2.imread(str(a.images/r['image']));h,w=image.shape[:2];lights=[];started=time.perf_counter()
  # Keep native 640x384 detail instead of reducing the full 1280x720 frame.
  for x in (0,w-640):
   result=model.predict(image[:384,x:x+640],classes=[a.class_id],imgsz=640,conf=.15,verbose=False)[0]
   for b,c in zip(result.boxes.xyxy.cpu().tolist(),result.boxes.conf.cpu().tolist()):lights.append({'label':'traffic_light','confidence':c,'bbox':[b[0]+x,b[1],b[2]+x,b[3]]})
  ordered=sorted(lights,key=lambda p:p['confidence'],reverse=True);keep=[]
  for p in ordered:
   if not any(overlap(p['bbox'],q['bbox'])>.4 for q in keep):keep.append(p)
  out[r['id']]={'crops':keep,'crop_ms':(time.perf_counter()-started)*1000}
  if k%20==0:print(k,len(records),flush=True)
 a.output.write_text(json.dumps(out));print('DONE',len(out),flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--model',required=True);p.add_argument('--class-id',type=int,default=9);p.add_argument('--annotations',type=Path,required=True);p.add_argument('--images',type=Path,required=True);p.add_argument('--output',type=Path,required=True);main(p.parse_args())
