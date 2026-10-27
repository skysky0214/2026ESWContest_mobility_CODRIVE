"""Opt-in sampled host-wall spans; asynchronous bounded logging, no GPU sync.

Enable with CODRIVE_LATENCY_FILE. Disabled mode makes no worker or output file.
Capture below is application receipt, never sensor exposure or audible sound.
"""
import atexit
import functools
import json
import os
import queue
import threading
import time

_ENABLED = bool(os.environ.get('CODRIVE_LATENCY_FILE'))
_LOCAL = threading.local()
_QUEUE = queue.Queue(maxsize=2048)
_DROPPED = 0
_COUNT = 0
_SESSION = os.environ.get('CODRIVE_LATENCY_SESSION', '')
_EVERY = max(1, int(os.environ.get('CODRIVE_LATENCY_EVERY', '10')))


def _writer():
    path=os.environ['CODRIVE_LATENCY_FILE']
    with open(path,'a',buffering=65536) as out:
        next_flush=time.monotonic()+1
        while True:
            try:
                row=_QUEUE.get(timeout=1)
            except queue.Empty:
                out.flush()
                continue
            if row is None:
                out.flush()
                return
            out.write(json.dumps(row,separators=(',',':'))+'\n')
            if time.monotonic()>=next_flush:
                out.flush(); next_flush=time.monotonic()+1


if _ENABLED:
    _THREAD=threading.Thread(target=_writer,name='latency-log',daemon=True)
    _THREAD.start()
    def _shutdown():
        try:_QUEUE.put_nowait(None)
        except queue.Full:return
        _THREAD.join(timeout=2)
    atexit.register(_shutdown)


def begin(kind, *, force=False):
    global _COUNT
    if not _ENABLED:return None
    _COUNT+=1
    if not force and _COUNT % _EVERY:return None
    row={'session':_SESSION,'kind':kind,'wall_start_ns':time.time_ns(),
         'start_ns':time.perf_counter_ns(),'spans':[],'links':{}}
    _LOCAL.row=row
    _LOCAL.stack=[]
    return row


def link(**values):
    row=getattr(_LOCAL,'row',None)
    if row is not None:row['links'].update(values)


def finish(row):
    global _DROPPED
    if row is None:return
    row['end_ns']=time.perf_counter_ns()
    row['wall_end_ns']=time.time_ns()
    row['dropped_records']=_DROPPED
    _LOCAL.row=None
    try:_QUEUE.put_nowait(row)
    except queue.Full:_DROPPED+=1


def header_key(h):
    return f'{h.source_id}:{h.boot_id}:{h.sequence}'


def wrap(cls,name,group,leaf):
    if not _ENABLED:return
    original=getattr(cls,name)
    if getattr(original,'_latency_wrapped',False):return
    @functools.wraps(original)
    def measured(*args,**kwargs):
        row=getattr(_LOCAL,'row',None)
        if row is None:return original(*args,**kwargs)
        index=len(row['spans']);parent=_LOCAL.stack[-1] if _LOCAL.stack else None
        span={'group':group,'leaf':leaf,'parent':parent,'start_ns':time.perf_counter_ns()}
        row['spans'].append(span);_LOCAL.stack.append(index)
        try:return original(*args,**kwargs)
        finally:
            span['end_ns']=time.perf_counter_ns();_LOCAL.stack.pop()
    measured._latency_wrapped=True
    setattr(cls,name,measured)


def install_front():
    if not _ENABLED:return
    from .sensor_input.external.front_perception import UltralyticsFrontPerception,YoloFrontPublisher
    from .hardware.camera.rectify import CameraRectifier
    from .sensor_input.external.lane.ufld import UfldLaneDetector
    wrap(UltralyticsFrontPerception,'detections','external_perception','yolo_inference_decode')
    wrap(UltralyticsFrontPerception,'perception_from_frame','external_perception','objects_signal_postprocess')
    wrap(YoloFrontPublisher,'publish_frame','external_perception','geometry_dds_publication')
    wrap(CameraRectifier,'apply','external_perception','lens_rectification')
    wrap(UfldLaneDetector,'detect','external_perception','lane_inference')


def install_pi():
    if not _ENABLED:return
    from .runtime import PiOrchestrator
    from .fusion import SituationFusion
    from .risk import RiskEngine
    from .team_core.risk.engine import RiskEngine as TeamEngine
    from .risk_ml.shadow import ShadowRisk
    wrap(PiOrchestrator,'_receive_inputs','fusion','dds_receive_decode')
    wrap(SituationFusion,'snapshot','fusion','snapshot_creation')
    wrap(RiskEngine,'evaluate','risk_decision','live_adapter_temporal_conditions')
    wrap(TeamEngine,'assess','risk_decision','candidates_fsm')
    for name in ('_objects','_road','_driver_and_audio','_acceleration'):
        wrap(TeamEngine,name,'risk_decision',name.lstrip('_'))
    wrap(ShadowRisk,'update','xgboost_parallel','feature_adaptation_dispatch')
    from .risk_ml import features_v3, infer
    wrap(infer.Predictor,'predict','xgboost_parallel','prediction_and_explanation')
    wrap(features_v3,'vector','xgboost_parallel','feature_encoding')
    wrap(features_v3,'probabilities','xgboost_parallel','booster_prediction')


def record(kind, links, start_ns=None, end_ns=None):
    """Rare cross-thread events (speech / localhost browser), no blocking I/O."""
    global _DROPPED
    if not _ENABLED:return
    now=time.time_ns()
    row={'session':_SESSION,'kind':kind,'wall_start_ns':start_ns or now,
         'wall_end_ns':end_ns or now,'links':links,'spans':[],'dropped_records':_DROPPED}
    try:_QUEUE.put_nowait(row)
    except queue.Full:_DROPPED+=1


def enabled():
    return _ENABLED


def install_cabin():
    if not _ENABLED:return
    from .sensor_input.driver.dms import UltralyticsYoloDms,YoloDmsEstimator
    wrap(UltralyticsYoloDms,'state_from_frame','driver_perception','state_postprocessing')
    wrap(UltralyticsYoloDms,'detections','driver_perception','yolo_inference_decode')
    wrap(YoloDmsEstimator,'observe','driver_perception','behavior_conditions')


def install_audio():
    if not _ENABLED:return
    from .sensor_input.audio import logmel,model
    wrap(logmel.LogMelClassifier,'classify','audio_perception','classification_filtering')
    wrap(logmel,'log_mel_spectrogram','audio_perception','logmel_features')
    wrap(model.OnnxRuntimeBackend,'predict_probabilities','audio_perception','onnx_inference')
    wrap(model.TorchCheckpointBackend,'predict_probabilities','audio_perception','torch_inference')
