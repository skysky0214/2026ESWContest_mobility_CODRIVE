"""Measured host-stage hierarchy in seconds and percentages; never fills absent stages with zero."""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
import statistics


def stats(values):
    values=sorted(values)
    if not values:return None
    def q(p):
        position=(len(values)-1)*p; i=int(position); f=position-i
        return values[i]*(1-f)+values[min(i+1,len(values)-1)]*f
    return {'samples':len(values),'mean_s':statistics.mean(values),'p50_s':q(.5),
            'p95_s':q(.95),'p99_s':q(.99),'max_s':values[-1]}


def hierarchy(rows):
    by_kind=defaultdict(list)
    for r in rows:
        if 'start_ns' in r and 'end_ns' in r:by_kind[r['kind']].append(r)
    output=[]
    for kind,traces in by_kind.items():
        totals=[]; groups=defaultdict(list); leaves=defaultdict(list)
        for trace in traces:
            duration=(trace['end_ns']-trace['start_ns'])/1e9
            if duration<=0:continue
            totals.append(duration)
            gs=defaultdict(float);ls=defaultdict(float)
            roots_ns=0
            spans=trace['spans']
            for i,span in enumerate(spans):
                inclusive_ns=span['end_ns']-span['start_ns']
                children_ns=sum(s['end_ns']-s['start_ns'] for s in spans if s['parent']==i)
                exclusive=(inclusive_ns-children_ns)/1e9
                if exclusive < -1e-8:raise ValueError('Overlapping or invalid child spans')
                gs[span['group']]+=max(0.,exclusive)
                ls[(span['group'],span['leaf'])]+=max(0.,exclusive)
                if span['parent'] is None:roots_ns+=inclusive_ns
            remainder=(trace['end_ns']-trace['start_ns']-roots_ns)/1e9
            if remainder < -1e-8:raise ValueError('Root spans exceed trace duration')
            if remainder>0:
                gs['shared_runtime']+=remainder
                ls[('shared_runtime','loop_preprocess_preview_dispatch')]+=remainder
            for g,v in gs.items():groups[g].append(v)
            for k,v in ls.items():leaves[k].append(v)
        total=stats(totals)
        output.append({'path':kind,'level':'전체','group':'','leaf':'',**total,'total_pct':100.,'group_pct':100.})
        for g,values in groups.items():
            metric=stats(values+[0.]*(len(totals)-len(values)))
            output.append({'path':kind,'level':'중분류','group':g,'leaf':'',**metric,
                           'total_pct':metric['mean_s']/total['mean_s']*100,'group_pct':100.})
        for (g,leaf),values in leaves.items():
            metric=stats(values+[0.]*(len(totals)-len(values))); parent=stats(groups[g]+[0.]*(len(totals)-len(groups[g])))
            output.append({'path':kind,'level':'끝단분류','group':g,'leaf':leaf,**metric,
                           'total_pct':metric['mean_s']/total['mean_s']*100,
                           'group_pct':metric['mean_s']/parent['mean_s']*100 if parent['mean_s'] else None})
    return output


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args()
    rows=[]
    for path in a.directory.glob('*.jsonl'):
        for line in path.open():
            try:rows.append(json.loads(line))
            except ValueError:pass
    phases_path=a.directory/'phases.json'
    if phases_path.exists():
        phase=next(v for v in json.loads(phases_path.read_text()) if v['phase']=='main')
        rows=[r for r in rows if phase['start_ns'] <= r.get('wall_start_ns',0) <= phase['end_ns']]
    result=hierarchy(rows)
    fields=['path','level','group','leaf','samples','mean_s','total_pct','group_pct','p50_s','p95_s','p99_s','max_s']
    with (a.directory/'hierarchy.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(result)
    (a.directory/'hierarchy.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print('Wrote',len(result),'hierarchy rows')


if __name__=='__main__':main()
