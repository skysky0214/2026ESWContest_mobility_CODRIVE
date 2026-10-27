import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('latency_report',Path(__file__).parents[2]/'tools/latency/report.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def test_nested_exclusive_times_and_cached_lane_frame_do_not_double_count():
    rows=[{'kind':'front','start_ns':0,'end_ns':1000000000,'spans':[
        {'group':'external','leaf':'pipeline','parent':None,'start_ns':100000000,'end_ns':900000000},
        {'group':'external','leaf':'yolo','parent':0,'start_ns':200000000,'end_ns':600000000},
        {'group':'lane','leaf':'ufld','parent':None,'start_ns':900000000,'end_ns':1000000000}]},
        {'kind':'front','start_ns':0,'end_ns':1000000000,'spans':[]}]
    out=m.hierarchy(rows)
    assert abs(sum(r['total_pct'] for r in out if r['level']=='중분류')-100)<1e-8
    assert abs(sum(r['total_pct'] for r in out if r['level']=='끝단분류')-100)<1e-8
    lane=next(r for r in out if r['leaf']=='ufld')
    assert lane['mean_s']==.05 and lane['total_pct']==5
    assert out[0]['mean_s']==1 and out[0]['total_pct']==100
