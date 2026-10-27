"""Join real frame IDs across hosts. Negative/ambiguous clock order is rejected."""
import argparse
import csv
import json
from pathlib import Path
from report import hierarchy, stats


def load(directory):
    rows=[]
    for p in directory.glob('*.jsonl'):
        for line in p.open():
            try:rows.append(json.loads(line))
            except ValueError:pass
    phases=json.loads((directory/'phases.json').read_text())
    phase=next(p for p in phases if p['phase']=='main')
    return [r for r in rows if phase['start_ns'] <= r.get('wall_start_ns',0) <= phase['end_ns']]


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args()
    rows=load(a.directory)
    frames={r['links']['frame']:r for r in rows if r['kind'] in ('front_frame','driver_frame')}
    receives={}
    for r in rows:
        if r['kind']=='sensor_receive':receives.setdefault(r['links']['sample'],r['wall_start_ns'])
    pi={r['links']['snapshot_time_ns']:r for r in rows if r['kind']=='state_publish'}
    combined=[]; joins=[];missing=negative=0
    for browser in (r for r in rows if r['kind']=='browser_render'):
        b=browser['links']; stamp=b.get('snapshot_time_ns',0)
        # Pi cycle begins immediately after that exact dashboard timestamp.
        cycle=pi.get(stamp)
        if cycle is None:
            # JSON passes nanoseconds through a JavaScript double (2**53 limit).
            # Allow <=1 microsecond of integer rounding, never a whole Pi cycle.
            candidates=[r for t,r in pi.items() if abs(t-stamp)<=1000]
            if len(candidates)==1:cycle=candidates[0]
        if cycle is None:missing+=1;continue
        for key in b.get('inputs',[]):
            edge=frames.get(key);receive=receives.get(key)
            if edge is None or receive is None:missing+=1;continue
            start=edge['links'].get('receipt_ns')
            publish=edge['links'].get('dds_publish_start_ns',edge['wall_end_ns'])
            end=b.get('browser_wall_ns')
            pi_end=cycle['wall_end_ns'];pi_begin=max(receive,cycle['wall_start_ns'])
            stamps=[start,publish,receive,pi_begin,pi_end,end]
            if not all(isinstance(v,int) for v in stamps) or any(x>y for x,y in zip(stamps,stamps[1:])):
                negative+=1;continue
            out={'kind':'front_result_to_hmi' if edge['kind']=='front_frame' else 'driver_result_to_hmi',
                 'start_ns':start,'end_ns':end,'spans':[]}
            for group,leaf,left,right in (
                ('external_perception' if edge['kind']=='front_frame' else 'driver_perception','frame_receipt_to_publish',start,publish),
                ('transport','publish_serialize_dds_receive',publish,receive),
                ('fusion','waiting_for_snapshot_used_by_browser',receive,pi_begin),
                ('pi_processing','fusion_risk_dashboard',pi_begin,pi_end),
                ('hmi','poll_wait_transfer_two_raf',pi_end,end)):
                out['spans'].append(dict(group=group,leaf=leaf,parent=None,start_ns=left,end_ns=right))
            # Decompose the exact source frame used by this browser update.
            # Its local monotonic spans are mapped using its own wall/monotonic anchor.
            mapping={}
            source_group=out['spans'][0]['group']
            for index,span in enumerate(edge['spans']):
                left=max(start,edge['wall_start_ns']+span['start_ns']-edge['start_ns'])
                right=min(publish,edge['wall_start_ns']+span['end_ns']-edge['start_ns'])
                if right<=left:continue
                parent=mapping.get(span['parent'],0)
                mapping[index]=len(out['spans'])
                out['spans'].append(dict(group=source_group,leaf=span['leaf'],parent=parent,
                                         start_ns=left,end_ns=right))
            combined.append(out)
            joins.append({'path':out['kind'],'frame':key,'event_id':b.get('event_id'),
                          'total_s':(end-start)/1e9,'browser_event':'two RAF opportunities, not physical display'})
    metrics=hierarchy(combined)
    fields=['path','level','group','leaf','samples','mean_s','total_pct','group_pct','p50_s','p95_s','p99_s','max_s']
    with (a.directory/'end-to-end-hierarchy.csv').open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(metrics)
    summary={'metrics':metrics,'matched_rows':len(joins),'missing_or_unsampled':missing,'clock_order_rejected':negative,
             'scope':'Measured selected sensor result to localhost browser rendering opportunity. Does not establish that the input caused the displayed warning.',
             'limitations':['Application receipt excludes physical exposure, readout and earlier capture buffers',
                            'No physical display photon or acoustic onset measurement',
                            'Cross-host values retain clock synchronization uncertainty; negatives are rejected, not clamped',
                            'XGBoost runs in parallel and is not summed into the warning path',
                            'Small-sample P99 is exploratory, not a tail-latency guarantee']}
    (a.directory/'end-to-end.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (a.directory/'matched-frames.json').write_text(json.dumps(joins,ensure_ascii=False,indent=2)+'\n')
    print('Joined',len(joins),'rows; rejected clock order',negative)


if __name__=='__main__':main()
