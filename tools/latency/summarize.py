"""Publish measured hierarchy, sample coverage and explicit unavailable endpoints."""
import argparse
from collections import Counter,defaultdict
import csv
import json
from pathlib import Path
from report import stats

PATHS={'front_frame':'외부 카메라 처리','driver_frame':'내부 카메라 처리','audio_window':'음향 분석 윈도우 처리',
       'pi_cycle':'Pi 처리 루프','xgboost_worker':'XGBoost 비동기 예측',
       'front_result_to_hmi':'외부 프레임 수신→HMI 상태 반영 기회',
       'driver_result_to_hmi':'내부 프레임 수신→HMI 상태 반영 기회'}
NAMES={'external_perception':'외부 인식','driver_perception':'운전자 인식','audio_perception':'음향 인식',
 'shared_runtime':'공통 처리','fusion':'수신·융합','risk_decision':'위험 판단','xgboost_parallel':'XGBoost 병렬 경로',
 'transport':'발행·DDS 수신','pi_processing':'Pi 처리','hmi':'HMI 전달·렌더링 대기',
 'lens_rectification':'렌즈 보정','lane_inference':'차선 추론','yolo_inference_decode':'YOLO 호출·결과 변환',
 'geometry_dds_publication':'객체 구성·발행 잔여 처리','objects_signal_postprocess':'객체·신호등·색 기반 차선 후처리',
 'loop_preprocess_preview_dispatch':'루프·전처리·정상 HMI 전달 등 잔여 처리',
 'classification_filtering':'음향 분류·시간 필터 잔여 처리','logmel_features':'로그멜 특성 추출',
 'torch_inference':'Torch 음향 추론','onnx_inference':'ONNX 음향 추론',
 'state_postprocessing':'운전자 상태 구성','behavior_conditions':'운전자 행동 조건 처리',
 'dds_receive_decode':'DDS 수신·JSON 해석','snapshot_creation':'스냅샷 생성',
 'live_adapter_temporal_conditions':'입력 유효성·지속 시간·모델 변환 잔여 처리','candidates_fsm':'위험 후보·FSM 잔여 처리',
 'objects':'객체 위험 후보','road':'신호·차선 위험 후보','feature_adaptation_dispatch':'XGBoost 특성 적응·작업 전달',
 'prediction_and_explanation':'예측·설명 잔여 처리','feature_encoding':'특성 인코딩','booster_prediction':'부스터 예측',
 'frame_receipt_to_publish':'프레임 수신→발행 잔여 처리','publish_serialize_dds_receive':'직렬화·DDS 전달·수신 해석',
 'waiting_for_snapshot_used_by_browser':'화면이 사용한 스냅샷까지의 대기','fusion_risk_dashboard':'융합·판단·HMI 상태 구성',
 'poll_wait_transfer_two_raf':'폴링 대기·HTTP 전달·두 RAF 시점까지',
 'driver_and_audio':'운전자·음향 위험 후보','acceleration':'급가속 위험 후보'}


def main():
 p=argparse.ArgumentParser();p.add_argument('directory',type=Path);a=p.parse_args();d=a.directory
 local=json.loads((d/'hierarchy.json').read_text());e2e=json.loads((d/'end-to-end.json').read_text());over=json.loads((d/'overhead.json').read_text())
 metrics=e2e['metrics']+local
 for path in {r['path'] for r in metrics}:
  part=[r for r in metrics if r['path']==path]
  for level in ('중분류','끝단분류'):
   assert abs(sum(r['total_pct'] for r in part if r['level']==level)-100)<1e-5,(path,level)
 fields=['측정 경로','분류 수준','중분류','끝단 분류','평균(초)','전체 대비(%)','중분류 대비(%)','P50(초)','P95(초)','P99(초)','최대(초)','표본 수']
 with (d/'측정결과.csv').open('w',newline='',encoding='utf-8-sig') as f:
  w=csv.writer(f);w.writerow(fields)
  for r in metrics:w.writerow([PATHS.get(r['path'],r['path']),r['level'],NAMES.get(r['group'],r['group']),NAMES.get(r['leaf'],r['leaf']),
      r['mean_s'],r['total_pct'],r['group_pct'],r['p50_s'],r['p95_s'],r['p99_s'],r['max_s'],r['samples']])
 phase=next(r for r in json.loads((d/'phases.json').read_text()) if r['phase']=='main')
 rows=[]
 for path in d.glob('*.jsonl'):
  for line in path.open():
   try:r=json.loads(line)
   except ValueError:continue
   if phase['start_ns']<=r.get('wall_start_ns',0)<=phase['end_ns']:rows.append(r)
 counts=Counter(r['kind'] for r in rows)
 warnings=[r for r in rows if r['kind']=='warning_created']
 starts=[r for r in rows if r['kind']=='speech_start_call'];completions=[r for r in rows if r['kind']=='speech_complete']
 speech_wait=[r['links']['queue_prepare_s'] for r in starts]
 warning_map={r['links']['event_id']:r for r in warnings}
 alert_render=[]
 for r in rows:
  if r['kind']!='browser_render':continue
  warning=warning_map.get(r['links'].get('event_id'))
  if warning:
   age=(r['links']['browser_wall_ns']-warning['links']['created_ns'])/1e9
   if age>=0 and not any(v['event_id']==r['links']['event_id'] for v in alert_render):
    alert_render.append({'event_id':r['links']['event_id'],'seconds':age})
 coverage={'rows_by_kind':dict(counts),'warnings':len(warnings),'speech_start_calls':len(starts),
  'speech_completions':len(completions),'warning_to_render':stats([r['seconds'] for r in alert_render]),
  'command_to_paplay_call':stats(speech_wait),
  'observed_warning_codes':dict(Counter(r['links']['risk_code'] for r in warnings)),
  'hardware_capture_to_app':None,'physical_display_onset':None,'physical_acoustic_onset':None,
  'physical_camera_distance_ttc':None,'can_transmitter_to_pi_receiver':None,'weather_http_fetch':None}
 unavailable=[
  ('외부 카메라 취득','노출·GMSL·캡처 버퍼','센서 노출 타임스탬프와 호스트 시각 연결 없음'),
  ('차량 입력','CAN 송신기→Pi 수신','송신 시각·패킷 대응 계측 없음'),
  ('위험 판단','실물 거리·TTC·bicycle 충돌 경고','현재 실물 카메라 위치의 거리 보정이 유효하지 않음'),
  ('환경 입력','기상 API HTTP 조회','백그라운드 조회의 구간 타이머 없음'),
  ('음향 입력','윈도우 이전 USB 수신·필터·리샘플링','현재 오디오 타이머는 분석 윈도우 준비 후 시작'),
  ('HMI','물리 화면 발광','브라우저 렌더링 기회까지만 계측'),
  ('음성','실제 스피커 음향 발생','소프트웨어 재생 호출까지만 계측 가능')]
 if not starts:unavailable.append(('음성','명령 생성→재생 호출','본 측정에서 실제 재생 이벤트 표본 없음'))
 with (d/'미측정항목.csv').open('w',newline='',encoding='utf-8-sig') as f:
  w=csv.writer(f);w.writerow(['중분류','끝단 분류','평균(초)','전체 대비(%)','상태','이유'])
  for group,leaf,why in unavailable:w.writerow([group,leaf,'','','미측정',why])
 (d/'coverage.json').write_text(json.dumps(coverage,ensure_ascii=False,indent=2)+'\n')
 lines=['# 실제 카메라 레이턴시 측정','',
 '유효 결과는 이 폴더의 clean 세션이다. 이전 예비/참고 세션은 집계에 섞지 않았다.',
 '', '## 측정 범위','',
 '카메라 프레임을 애플리케이션이 받은 시점부터 측정한다. HMI 끝점은 Pi 브라우저의 두 번의 requestAnimationFrame 이후 시점이다. 실제 센서 노출, 화면 발광, 스피커 소리 발생 시점은 측정하지 않았다.',
 '', '아래 HMI 수치는 센서 결과가 포함된 상태의 소프트웨어 반영 경로다. 해당 프레임이 위험 경고의 원인이었다고 입증하는 값과는 구분한다.',
 '', '## 전체 시간','', '| 경로 | 평균(초) | P50(초) | P95(초) | P99(초) | 표본 |','|---|---:|---:|---:|---:|---:|']
 for r in metrics:
  if r['level']=='전체':lines.append(f"| {PATHS.get(r['path'],r['path'])} | {r['mean_s']:.6f} | {r['p50_s']:.6f} | {r['p95_s']:.6f} | {r['p99_s']:.6f} | {r['samples']} |")
 lines+=['','## 중분류·끝단 분류','',
 '전체 행은 해당 경로의 100%이다. 중분류·끝단 분류의 전체 대비 비율은 동일 경로의 평균 시간을 분모로 삼는다. 끝단의 중분류 대비 비율도 CSV에 기록했다. 중첩 호출은 직접 자식의 시간을 빼서 중복 합산하지 않는다. 캐시로 생략된 차선 추론은 그 프레임에서 실행되지 않은 것으로 계산하며, 측정 불가 항목을 0으로 채운 것은 아니다.',
 '', '카메라·음향·Pi·XGBoost의 별도 전체 행은 병렬 경로이므로 서로 더하지 않는다. Pi 세부 시간은 자체 처리 루프 기준이며 전체 HMI 경로에서는 Pi 처리 구간에 해당한다. P95·P99를 더해서 전체 지연을 만들지 않았다.',
 '', '## 계측 부하와 상태','', '| 프로세스 | OFF CPU(%) | ON CPU(%) | 차이(%p) |','|---|---:|---:|---:|']
 for r in over['summary']:lines.append(f"| {r['unit']} | {r['off_cpu_pct']:.2f} | {r['on_cpu_pct']:.2f} | {r['difference_percentage_points']:+.2f} |")
 lines+=['', 'CPU 100%는 코어 하나 기준이다. 이 차이는 두 번씩의 짧은 A/B 관찰치이며 지연 증가율을 의미하지 않는다. 장면·시스템 변동이 남아 있으므로 계측만의 인과 효과로 단정하지 않는다.',
 '', '디버그 스트림·오버레이, 상세 프레임 JSON 로그, 음향 상세 진단, CARLA→Jetson 영상 전송, 시나리오 기록기의 10Hz Pi API 조회는 껐다. 정상 HMI 영상·센서·CAN·판단 경로는 유지했다. 추가 GPU 동기화 호출을 넣지 않았고, 기록은 크기가 제한된 큐와 별도 저장 스레드를 사용했다.',
 '', '브라우저 CPU는 Snap cgroup 분리 때문에 /proc의 Firefox 부모 프로세스 시간만 사용했다. 렌더러·GPU 하위 프로세스 전체 CPU는 포함하지 않는다.',
 '', '미측정 CPU 항목: '+json.dumps(over.get('unavailable_units',[]),ensure_ascii=False),
 '', '기록 큐 초과 누락 최댓값: '+json.dumps(over['max_dropped_measurement_records']),
 '', '## 실제 경고·음성 표본','',
 f"본 측정의 경고 생성 {len(warnings)}건, 음성 시작 호출 {len(starts)}건, 음성 완료 {len(completions)}건.",
 '코드별 경고: '+json.dumps(coverage['observed_warning_codes'],ensure_ascii=False),
 '경고 생성→브라우저 반영: '+json.dumps(coverage['warning_to_render'],ensure_ascii=False),
 '명령 생성→paplay 호출: '+json.dumps(coverage['command_to_paplay_call'],ensure_ascii=False),
 '', '음성 시작 호출은 소프트웨어 경계이며 실제 소리가 난 시점은 아니다. 표본이 없으면 해당 지연은 미측정이다.',
 '', '## 미측정·해석 제한','',
 '- 실물 카메라 위치의 거리 보정이 유효하지 않아 CARLA 가상 카메라 보정을 차단했다. 거리·TTC·bicycle 충돌 후보의 유효한 실물 경고 지연은 미측정이다.',
 '- 카메라 노출·GMSL·캡처 버퍼, CAN 송신기부터 Pi까지, 외부 API HTTP 조회 지연은 이번 타임스탬프로 분해하지 못했다.',
 '- XGBoost는 표시용 비동기 경로이며 위험 경고의 직렬 경로에 합산하지 않는다.',
 '- 졸음 2초·주의분산 3초 등의 정책 확인 대기는 처리 시간과 구분한다. 실제 조건 시작 시점이 추적되지 않은 전이는 설정값을 실측 지연으로 바꾸지 않는다.',
 '- YOLO 호출 시간은 전처리·추론 대기·결과 변환을 포함한 호스트 관측 시간이다. 순수 GPU 커널 시간은 아니다.',
 '- 보드 간 구간은 동기화된 벽시계로 계산했다. 측정 후 SSH 시계 진단의 오차 범위를 clock-bounds.json에 남겼다. 이것은 측정 구간 전체의 하드웨어 시각 보증이 아니다.',
 '- 작은 표본의 P99와 이번 정적 시연 장면은 최악 조건이나 실제 주행 안전성을 보장하지 않는다.',
 '', '## 파일','',
 '- `미측정항목.csv`: 타임스탬프·보정·이벤트 부족 항목과 이유',
 '- `측정결과.csv`: 전체·중분류·끝단 초/%와 분위수',
 '- `hierarchy.csv/json`: 보드 내부 구간',
 '- `end-to-end-hierarchy.csv`, `end-to-end.json`: 프레임 ID로 연결한 HMI 경로',
 '- `overhead.json`, `phases.json`: OFF/ON 반복과 프로세스 부하',
 '- `clean-audit.json`, `preflight.json`, `postflight.json`, `clock-bounds.json`: 조건·시계 확인',
 '- `front/driver/audio/pi.jsonl`: 원시 계측. 세션·프레임·이벤트 ID 포함',
 '']
 (d/'결과요약.md').write_text('\n'.join(lines))
 print('Wrote Korean results and coverage',coverage)


if __name__=='__main__':main()
