import importlib.util
import os
from pathlib import Path
import time


def load(monkeypatch, path=None):
    if path: monkeypatch.setenv('CODRIVE_LATENCY_FILE',str(path))
    else: monkeypatch.delenv('CODRIVE_LATENCY_FILE',raising=False)
    monkeypatch.setenv('CODRIVE_LATENCY_EVERY','1')
    spec=importlib.util.spec_from_file_location('isolated_latency_probe',Path(__file__).parents[1]/'src/codrive/latency_probe.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m


def test_disabled_has_no_worker_or_span(monkeypatch):
    m=load(monkeypatch)
    assert m.begin('frame') is None and not m.enabled()


def test_nested_spans_preserve_parent_and_do_not_change_return_values(monkeypatch,tmp_path):
    m=load(monkeypatch,tmp_path/'trace.jsonl')
    class Pipeline:
        def child(self):return 42
        def parent(self):return self.child()
    m.wrap(Pipeline,'child','model','inference');m.wrap(Pipeline,'parent','model','pipeline')
    row=m.begin('frame')
    assert Pipeline().parent()==42
    m.finish(row)
    assert row['spans'][1]['parent']==0
    assert row['spans'][0]['start_ns'] <= row['spans'][1]['start_ns'] < row['spans'][1]['end_ns'] <= row['spans'][0]['end_ns']
    assert m._LOCAL.row is None


def test_full_buffer_drops_measurement_without_blocking_or_changing_pipeline(monkeypatch):
    m=load(monkeypatch)
    m._ENABLED=True
    for _ in range(m._QUEUE.maxsize):m._QUEUE.put_nowait({})
    start=time.perf_counter()
    row=m.begin('frame');m.finish(row)
    assert m._DROPPED==1 and time.perf_counter()-start < .1
