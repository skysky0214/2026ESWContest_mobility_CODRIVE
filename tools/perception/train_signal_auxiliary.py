"""Train a separate one-class head with bounded GPU memory; no live-model overwrite."""
import argparse,os
from pathlib import Path
os.environ['YOLO_OFFLINE']='true'
import torch
from ultralytics import YOLO
import ultralytics.engine.trainer as trainer
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--weights',required=True);p.add_argument('--output',required=True);a=p.parse_args()
 # Avoid the AMP probe's unrelated model download and large temporary batch.
 trainer.check_amp=lambda model:True
 torch.cuda.set_per_process_memory_fraction(.12,0)
 YOLO(a.weights).train(data=a.data,project=a.output,name='signal-auxiliary',epochs=30,imgsz=640,batch=1,workers=2,device=0,freeze=10,optimizer='AdamW',lr0=.001,patience=10,amp=True,plots=False,cache=False,seed=20261027,deterministic=True,mosaic=0.,mixup=0.,degrees=0.,translate=.03,scale=.1,hsv_h=.005,hsv_s=.1,hsv_v=.1,close_mosaic=0,save=True)
