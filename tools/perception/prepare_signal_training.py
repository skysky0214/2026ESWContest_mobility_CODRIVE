"""Create a signal-only auxiliary dataset; existing road-user detector is frozen."""
import argparse,json
from pathlib import Path
import cv2,numpy as np

def main(a):
 records=[json.loads(s) for s in (a.dataset/'corrected-annotations.jsonl').read_text().splitlines()];root=a.output.resolve();root.mkdir(parents=True,exist_ok=True);manifest=[]
 for r in records:
  if r['split']=='test':continue # Held-out intersection is never used by optimizer/early stopping.
  split='train' if r['split']=='development' else 'val'
  image=cv2.imread(str(a.dataset/r['image']));polygon=np.asarray(json.loads((a.dataset/'refined-profile.json').read_text())['valid_image_polygon'],np.int32)
  mask=np.zeros(image.shape[:2],np.uint8);cv2.fillPoly(mask,[polygon],255);image[mask==0]=114
  for i,x in enumerate((0,640)):
   patch=image[:384,x:x+640];labels=[]
   for g in r['gt']['labels']:
    if g['class']!='traffic_light':continue
    a1,b1,a2,b2=g['bbox'];cx=(a1+a2)/2
    if not x<=cx<x+640:continue
    left,top,right,bottom=max(0,a1-x),max(0,b1),min(640,a2-x),min(384,b2)
    if right-left<2 or bottom-top<4:continue
    labels.append('0 %.7f %.7f %.7f %.7f'%((left+right)/1280,(top+bottom)/768,(right-left)/640,(bottom-top)/384))
   name=r['id']+'_'+str(i);ip=root/'images'/split/(name+'.jpg');lp=root/'labels'/split/(name+'.txt');ip.parent.mkdir(parents=True,exist_ok=True);lp.parent.mkdir(parents=True,exist_ok=True)
   cv2.imwrite(str(ip),patch,[cv2.IMWRITE_JPEG_QUALITY,98]);lp.write_text('\n'.join(labels)+'\n');manifest.append({'image':str(ip),'source':r['id'],'scene':r['case'],'split':split,'labels':len(labels)})
 (root/'data.yaml').write_text('path: '+str(root)+'\ntrain: images/train\nval: images/val\nnames:\n  0: traffic_light\n')
 (root/'manifest.json').write_text(json.dumps(manifest,indent=2));print('images',len(manifest),'train',sum(r['split']=='train' for r in manifest),'val',sum(r['split']=='val' for r in manifest))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('dataset',type=Path);p.add_argument('output',type=Path);main(p.parse_args())
