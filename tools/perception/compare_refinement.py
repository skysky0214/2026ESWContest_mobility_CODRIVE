"""Validation-only threshold selection and frozen candidate construction."""
import argparse,json,sys
from pathlib import Path
import cv2,numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))
from codrive.sensor_input.external.signal_refinement import merge_signals,SignalRefinement
from codrive.sensor_input.driver.dms import YoloDetection
from annotation_registration import registration,screen_polygon
from evaluate_dataset import score

def build(records,crops,profile,root):
 refinement=SignalRefinement(profile);predictions={};colours={'correct':0,'incorrect':0,'unknown':0};tp=fp=fn=0
 for r in records:
  base=[YoloDetection(p['label'],p['confidence'],tuple(p['bbox'])) for p in r['baseline']['raw_detections']]
  extra=[YoloDetection(p['label'],p['confidence'],tuple(p['bbox'])) for p in crops[r['id']]['crops']]
  detections=refinement.filter(merge_signals(base,extra,refinement.minimum))
  if profile.get('minimum_state_evidence'):
   image=cv2.imread(str(root/r['image']))
   detections=tuple(p for p in detections if p.label!='traffic_light' or (refinement.state(image,p.bbox_xyxy)[0].value!='unknown' and refinement.state(image,p.bbox_xyxy)[1]>=profile['minimum_state_evidence']))
  ds=[{'label':p.label if p.label!='person' else 'pedestrian','confidence':p.confidence,'bbox':p.bbox_xyxy} for p in detections]
  good,bad,missing,outside=score(r['gt']['labels'],ds,r['gt']['ignored'],np.asarray(profile['valid_image_polygon']),.5)
  tp+=sum(p['label']=='traffic_light' for p,g,i in good);fp+=sum(p['label']=='traffic_light' for p in bad);fn+=sum(g['class']=='traffic_light' for g in missing)
  image=cv2.imread(str(root/r['image']))
  for p,g,i in good:
   if p['label']!='traffic_light':continue
   state,evidence=refinement.state(image,p['bbox']);key='unknown' if state.value=='unknown' else ('correct' if state.value==g['state'] else 'incorrect');colours[key]+=1
  predictions[r['id']]={'detections':ds,'crop_ms':crops[r['id']]['crop_ms']}
 return predictions,{'TP':tp,'FP':fp,'FN':fn,'precision':tp/(tp+fp) if tp+fp else None,'recall':tp/(tp+fn) if tp+fn else None,'matched_colour':colours}
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('dataset',type=Path);p.add_argument('crops',type=Path);p.add_argument('--minimum',type=float,default=.35);p.add_argument('--tune',action='store_true');p.add_argument('--gate',type=float,default=0.);p.add_argument('--profile',type=Path);a=p.parse_args()
 records=[json.loads(s) for s in (a.dataset/'corrected-annotations.jsonl').read_text().splitlines()];crops=json.loads(a.crops.read_text())
 base=json.loads((a.dataset.parent/'monitor-registration.json').read_text());first=next((r for r in records if r['split']=='development'),records[0])
 image=cv2.imread(str(a.dataset/first['image']));local=registration(image,base)
 profile={'schema_version':1,'image_size':[1280,720],'signal_min_confidence':a.minimum,'signal_crops':[[0,0,640,384],[640,0,1280,384]],'signal_layout':'vertical_three_lamp','valid_image_polygon':screen_polygon(base,local).tolist(),'limitations':['Fixed physical camera/monitor position only; recalibrate after movement.','Vertical three-lamp layout only.','Contrast evidence is not a calibrated probability.','No simulator truth used at runtime.']}
 profile['minimum_state_evidence']=a.gate
 if a.profile:profile=json.loads(a.profile.read_text())
 if a.tune:
  for minimum in (.3,.35,.4,.45,.5,.6):
   profile['signal_min_confidence']=minimum
   _,m=build([r for r in records if r['split']=='validation'],crops,profile,a.dataset);print(minimum,json.dumps(m),flush=True)
 else:
  predictions,m=build(records,crops,profile,a.dataset)
  (a.dataset/'refined-predictions.json').write_text(json.dumps(predictions));(a.dataset/'refined-profile.json').write_text(json.dumps(profile,indent=2)+'\n');print(json.dumps(m,indent=2))
