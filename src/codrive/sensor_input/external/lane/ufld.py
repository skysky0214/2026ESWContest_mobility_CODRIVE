"""TensorRT 10 UFLDv2 inference with upstream RGB/crop/normalization semantics."""
from dataclasses import dataclass
from pathlib import Path
import time
import cv2
import numpy as np
import torch
import tensorrt as trt
from .polyline import continuous_lane
from .illumination import lane_input

@dataclass(frozen=True)
class ModelLane:
    side: str
    points: tuple
    confidence: float
    slot_id: int = -1

class UfldLaneDetector:
    def __init__(self,path=None):
        self.logger=trt.Logger(trt.Logger.WARNING)
        self.runtime=trt.Runtime(self.logger)
        path=Path(path or 'artifacts/models/vision/ufld/culane_res18.engine')
        self.engine=self.runtime.deserialize_cuda_engine(path.read_bytes())
        if self.engine is None:raise RuntimeError('UFLD engine could not be loaded')
        self.context=self.engine.create_execution_context()
        self.tensors={}
        for i in range(self.engine.num_io_tensors):
            name=self.engine.get_tensor_name(i)
            shape=tuple(self.engine.get_tensor_shape(name))
            dtype=torch.float16 if self.engine.get_tensor_dtype(name)==trt.float16 else torch.float32
            tensor=torch.empty(shape,device='cuda',dtype=dtype)
            self.tensors[name]=tensor
            self.context.set_tensor_address(name,tensor.data_ptr())
        self.mean=np.array([.485,.456,.406],np.float32)
        self.std=np.array([.229,.224,.225],np.float32)
        self.last_ms=0
        self.stream=torch.cuda.Stream()
    def detect(self,frame):
        started=time.perf_counter()
        frame=lane_input(frame)
        # Upstream demo: resize RGB to 533x1600 then retain the bottom 320 rows.
        rgb=cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)
        rgb=cv2.resize(rgb,(1600,int(320/.6)),interpolation=cv2.INTER_LINEAR)[-320:]
        image=(rgb.astype(np.float32)/255-self.mean)/self.std
        tensor=torch.from_numpy(np.ascontiguousarray(image.transpose(2,0,1)[None]))
        with torch.cuda.stream(self.stream):
            self.tensors['input'].copy_(tensor)
            if not self.context.execute_async_v3(self.stream.cuda_stream):raise RuntimeError('UFLD inference failed')
            pred={k:v.float().cpu().numpy() for k,v in self.tensors.items() if k!='input'}
        lanes=[]
        colour_image=cv2.resize(frame,(640,360),interpolation=cv2.INTER_AREA)
        hsv=cv2.cvtColor(colour_image,cv2.COLOR_BGR2HSV)
        paint=cv2.inRange(hsv,(0,0,150),(180,80,255)) | cv2.inRange(hsv,(14,65,80),(42,255,255))
        paint=cv2.dilate(paint,np.ones((9,9),np.uint8))
        anchors=np.linspace(.42,1,72)
        # CULane's two ego-lane slots, not outer adjacent-lane slots.
        for index,side in [(1,'left'),(2,'right')]:
            existence=pred['exist_row'][0,:,:,index]
            delta=existence[1]-existence[0]
            probability=1/(1+np.exp(-np.clip(delta,-50,50)))
            valid=probability>.6
            if valid.sum()<=36:continue
            coords=[]
            for row in np.flatnonzero(valid):
                logits=pred['loc_row'][0,:,row,index]
                peak=int(logits.argmax())
                indices=np.arange(max(0,peak-1),min(199,peak+1)+1)
                weights=np.exp(logits[indices]-logits[indices].max());weights/=weights.sum()
                x=float((weights@indices+.5)/199)
                y=float(anchors[row])
                if 0<=x<=1:coords.append((x,y))
            coords=continuous_lane(coords)
            if len(coords)<8:continue
            supported=sum(bool(paint[min(359,round(y*359)),min(639,round(x*639))]) for x,y in coords)
            if supported<max(3,len(coords)*.10):continue
            lanes.append(ModelLane(side,tuple(coords),float(probability[valid].mean()),index))
        # UFLDv2 hybrid anchors: ego rows 1/2, adjacent columns 0/3.
        # https://github.com/cfzd/Ultra-Fast-Lane-Detection-v2/blob/master/demo.py
        anchors_col=np.linspace(0,1,pred['loc_col'].shape[2])
        grid_col=pred['loc_col'].shape[1]
        for index,side in [(0,'outer_left'),(3,'outer_right')]:
            existence=pred['exist_col'][0,:,:,index]
            probability=1/(1+np.exp(-np.clip(existence[1]-existence[0],-50,50)))
            valid=probability>.6
            if valid.sum()<=len(anchors_col)/4:continue
            coords=[]
            for col in np.flatnonzero(valid):
                logits=pred['loc_col'][0,:,col,index];peak=int(logits.argmax())
                indices=np.arange(max(0,peak-1),min(grid_col-1,peak+1)+1)
                weights=np.exp(logits[indices]-logits[indices].max());weights/=weights.sum()
                y=float((weights@indices+.5)/(grid_col-1));x=float(anchors_col[col])
                if 0<=y<=1:coords.append((x,y))
            coords=continuous_lane(sorted(coords,key=lambda p:p[1]))
            if len(coords)<8:continue
            supported=sum(bool(paint[min(359,round(y*359)),min(639,round(x*639))]) for x,y in coords)
            if supported<max(3,len(coords)*.1):continue
            lanes.append(ModelLane(side,tuple(coords),float(probability[valid].mean()),index))
        self.last_ms=(time.perf_counter()-started)*1000
        return lanes

def draw_model_lanes(frame,lanes,selected_slots=None):
    h,w=frame.shape[:2]
    for lane in lanes:
        coords=np.array([(round(x*w),round(y*(h-1))) for x,y in lane.points],np.int32)
        colour=(0,220,255) if selected_slots is None or lane.slot_id in selected_slots else (130,145,150)
        cv2.polylines(frame,[coords],False,colour,3,cv2.LINE_AA)
        x,y=coords[-1]
        label=("ego-left" if lane.slot_id==selected_slots[0] else "ego-right") if selected_slots and lane.slot_id in selected_slots else lane.side
        cv2.putText(frame,label,(int(x),min(h-10,int(y))),cv2.FONT_HERSHEY_SIMPLEX,.55,colour,1)
