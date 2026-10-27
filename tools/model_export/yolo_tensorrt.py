"""Build a fixed batch-1 FP16 TensorRT engine for this Jetson."""
import os
os.environ['YOLO_AUTOINSTALL']='False'
from ultralytics import YOLO
YOLO('yolo11n.pt').export(format='engine',imgsz=(384,640),device=0,quantize=16,workspace=0.5,batch=1,dynamic=False,simplify=False)
