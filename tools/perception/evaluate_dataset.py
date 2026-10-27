"""Offline class detection audit. Simulator labels never reach live perception."""
import argparse,json
from collections import defaultdict
from pathlib import Path
import cv2,numpy as np
from annotation_registration import registration,box_to_camera,screen_polygon

CLASSES=('pedestrian','bicycle','car','motorcycle','bus','truck','traffic_light','stop_sign')
def iou(a,b):
 x=max(0,min(a[2],b[2])-max(a[0],b[0]));y=max(0,min(a[3],b[3])-max(a[1],b[1]));inter=x*y
 return inter/max(1e-9,(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter)
def canonical(s):return {'person':'pedestrian'}.get(s.replace(' ','_'),s.replace(' ','_'))
def score(labels,predictions,ignored,polygon,threshold=.5):
 used=set();tp=[];fp=[];outside=[]
 for p in sorted(predictions,key=lambda p:p['confidence'],reverse=True):
  b=p['bbox'];centre=((b[0]+b[2])/2,(b[1]+b[3])/2)
  if cv2.pointPolygonTest(polygon.astype(np.float32),centre,False)<0:outside.append(p);continue
  candidates=[(iou(b,g['bbox']),i) for i,g in enumerate(labels) if i not in used and g['class']==p['label']]
  best=max(candidates,default=(0,-1))
  if best[0]>=threshold:used.add(best[1]);tp.append((p,labels[best[1]],best[0]))
  elif not any(g['class']==p['label'] and iou(b,g['bbox'])>.25 for g in ignored):fp.append(p)
 return tp,fp,[g for i,g in enumerate(labels) if i not in used],outside

def main(args):
 root=args.dataset;base=json.loads((root.parent/'monitor-registration.json').read_text());extra={}
 if args.predictions:extra=json.loads(args.predictions.read_text())
 counts=defaultdict(lambda:defaultdict(lambda:[0,0,0]));rows=[];corrected=[];rejected=[]
 gallery=root/('audit-'+args.name);gallery.mkdir(exist_ok=True)
 for line in (root/'annotations.jsonl').read_text().splitlines():
  r=json.loads(line);image=cv2.imread(str(root/r['image']))
  try:local=registration(image,base)
  except ValueError as e:rejected.append({'id':r['id'],'reason':str(e)});continue
  labels=[{**g,'bbox':box_to_camera(g['virtual_bbox'],base,local)} for g in r['gt']['labels']]
  ignored=[{**g,'bbox':box_to_camera(g['virtual_bbox'],base,local)} for g in r['gt']['ignored'] if 'virtual_bbox' in g]
  predictions=extra.get(r['id'],{}).get('detections') if args.predictions else r['baseline']['raw_detections']
  if predictions is None:raise ValueError('Predictions missing: '+r['id'])
  predictions=[{**p,'label':canonical(p['label'])} for p in predictions]
  # The live baseline removes traffic-light boxes below .60 before publication.
  if not args.predictions:predictions=[p for p in predictions if p['label']!='traffic_light' or p['confidence']>=.60]
  tp,fp,fn,outside=score(labels,predictions,ignored,screen_polygon(base,local),args.iou)
  split=r['split']
  for kind in CLASSES:
   c=[sum(p['label']==kind for p,g,i in tp),sum(p['label']==kind for p in fp),sum(g['class']==kind for g in fn)]
   for group in (split,'all'):
    counts[group][kind]=[a+b for a,b in zip(counts[group][kind],c)]
  rows.append({'id':r['id'],'case':r['case'],'split':split,'tp':len(tp),'fp':len(fp),'fn':len(fn),'out_of_scene':len(outside),'registration_error_px':local['marker_error_max_px']})
  corrected.append({**r,'gt':{**r['gt'],'labels':labels,'ignored':ignored},'registration':local})
  # One representative per scene, preserving independent-scene selection.
  if not any(x['case']==r['case'] for x in rows[:-1]):
   for p,g,i in tp:cv2.rectangle(image,tuple(map(int,p['bbox'][:2])),tuple(map(int,p['bbox'][2:])),(0,220,0),2)
   for p in fp:
    b=p['bbox'];cv2.rectangle(image,tuple(map(int,b[:2])),tuple(map(int,b[2:])),(0,0,255),2);cv2.putText(image,'FP '+p['label'],tuple(map(int,b[:2])),0,.4,(0,0,255),1)
   for g in fn:
    b=g['bbox'];cv2.rectangle(image,tuple(map(int,b[:2])),tuple(map(int,b[2:])),(255,80,0),1)
   cv2.putText(image,f"{r['case']} TP:{len(tp)} FP:{len(fp)} FN:{len(fn)}",(10,25),0,.5,(255,255,255),1)
   cv2.imwrite(str(gallery/(r['case']+'.jpg')),image)
 def metrics(c):
  t,p,n=c;return {'TP':t,'FP':p,'FN':n,'precision':t/(t+p) if t+p else None,'recall':t/(t+n) if t+n else None,'support':t+n}
 result={'name':args.name,'iou_threshold':args.iou,'accepted_frames':len(rows),'independent_scenes':len(set(r['case'] for r in rows)),'rejected_frames':rejected,'metrics':{s:{k:metrics(v) for k,v in group.items()} for s,group in counts.items()},'frames':rows,'limitations':['Stationary monitor recapture, not real-road accuracy.','Small lamp boxes are sensitive to 1-2 pixel registration error.','Stop sign has no guaranteed positive support.','Adjacent repetitions are correlated; splits are by intersection/distance, not random frames.']}
 (root/(args.name+'-metrics.json')).write_text(json.dumps(result,indent=2)+'\n')
 (root/'corrected-annotations.jsonl').write_text('\n'.join(json.dumps(r) for r in corrected)+'\n')
 print(json.dumps({k:v for k,v in result.items() if k!='frames'},indent=2))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('dataset',type=Path);p.add_argument('--name',default='baseline');p.add_argument('--predictions',type=Path);p.add_argument('--iou',type=float,default=.5);main(p.parse_args())
