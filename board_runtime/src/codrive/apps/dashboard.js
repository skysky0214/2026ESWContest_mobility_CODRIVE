const $=id=>document.getElementById(id);let lastGood=0;

let riskExpiryTimer;
const featureLabels={traffic_light:'신호등',pedestrian_count:'보행자 수',vehicle_count:'차량 수',vehicle_speed:'속도 (km/h)',accelerator_pct:'가속페달 (%)',brake_pressed:'브레이크',driver_drowsy:'졸음 추정',phone_usage:'휴대폰 사용',eyes_on_road:'전방 주시 추정',distraction_score:'주의 분산 점수',siren_detected:'사이렌',horn_detected:'경적',audio_direction:'음향 방향',weather:'날씨',is_night:'야간'};
Object.assign(featureLabels,{bicycle_count:'자전거 수',motorcycle_count:'오토바이 수',stop_sign_detected:'정지 표지판',central_object_present:'화면 중앙 객체',max_bbox_area:'최대 객체 박스 면적',driver_behavior:'운전자 행동',attention_score:'주의도',drowsiness_score:'졸음 점수',looking_forward:'전방 주시 추정',phone_detected:'휴대폰 감지',behavior_duration_ms:'행동 지속시간 (ms)',audio_kind:'음향 종류',speed_mps:'속도 (m/s)',speed_accel_mps2:'차속 기준 가속도 (m/s²)',accelerator_rate_pct_s:'페달 변화율 (%p/s)',acceleration_duration_ms:'급가속 지속시간 (ms)',pedal_rate_duration_ms:'급페달 지속시간 (ms)',precipitation_mm_h:'강수량 (mm/h)',snowfall_cm_h:'강설량 (cm/h)',visibility_m:'가시거리 (m)',front_valid:'외부 카메라 유효',driver_valid:'내부 카메라 유효',audio_valid:'음향 유효',speed_valid:'차속 유효',pedal_valid:'페달 유효',brake_valid:'브레이크 유효',weather_valid:'날씨 유효',daylight_valid:'주야간 유효'});
function renderRisk(model,now=Date.now()){
 clearTimeout(riskExpiryTimer);
 const panel=$('modelPanel');panel.className='card model';
 $('modelProbabilities').replaceChildren();$('modelInputs').replaceChildren();
 $('modelCoverage').textContent='입력 상태를 확인할 수 없습니다';
 $('modelMode').textContent='화면 표시 전용';
 if(!model){$('modelLevel').textContent='연결 확인 필요';$('modelMessage').textContent='모델 결과를 확인할 수 없습니다.';return;}
 const names={disabled:'모델 미연결',pending:'판단 중',unavailable:'판단 불가',model_error:'모델 연결 오류',expired:'입력 만료 · 판단 대기'};
 const valid=model.status==='ok'&&Number(model.valid_until_ns)/1e6>now;
 if(valid){
  riskExpiryTimer=setTimeout(()=>renderRisk({...model,status:'expired',inputs:Object.fromEntries(Object.keys(model.inputs||{}).map(key=>[key,null]))}),Math.max(0,Number(model.valid_until_ns)/1e6-now));
  const level={LOW:'NORMAL',MEDIUM:'CAUTION',HIGH:'WARNING'}[model.xgboost_risk_level];
  $('modelLevel').textContent=(level||'판단 불가')+(model.degraded?' · 입력 제한':'');
  if(level&&!model.degraded)panel.classList.add(level.toLowerCase());
  $('modelMessage').textContent=model.warning_message;
  for(const [name,value] of Object.entries(model.risk_probability||{})){
   const item=document.createElement('span');item.textContent=({LOW:'NORMAL',MEDIUM:'CAUTION',HIGH:'WARNING'}[name]||name)+' '+(value*100).toFixed(1)+'%';$('modelProbabilities').append(item);
  }
 }else{
  $('modelLevel').textContent=model.status==='ok'?'입력 만료 · 판단 대기':names[model.status]||'판단 불가';
  $('modelMessage').textContent=model.status==='disabled'?'모델이 연결되면 보조 위험도와 안내를 표시합니다.':['ok','expired'].includes(model.status)?'새 센서 결과를 기다리고 있습니다.':model.warning_message||'모델을 확인해 주세요. 기존 운전자 안내는 계속 동작합니다.';
 }
 const entries=Object.entries(model.inputs||{}),sensorEntries=entries.filter(([key])=>!key.endsWith('_valid')),count=sensorEntries.filter(([,value])=>value!==null).length;
 if(entries.length)$('modelCoverage').textContent='관측 입력 '+count+' / '+sensorEntries.length+' · 미수신·보류 값은 판단에서 누락됩니다';
 for(const [key,value] of entries){
  const row=document.createElement('div');row.className='row';
  const label=document.createElement('span');label.textContent=featureLabels[key]||key;
  const output=document.createElement('span');output.textContent=value===null?'미수신 / 보류':typeof value==='boolean'?(value?'예':'아니요'):typeof value==='number'?String(Number(value.toFixed(2))):String(value);
  if(value===null)output.className='missing';row.append(label,output);$('modelInputs').append(row);
 }
}
function offline(text){renderRisk(null);$('connection').textContent=text;$('hero').className='hero';$('level').textContent='데이터 연결 확인 필요';$('message').textContent='운전자 상태를 확인할 수 없습니다';$('description').textContent='연결이 복구되면 자동으로 갱신됩니다';for(const id of ['attention','drowsy','phone','presence','age','inference','audioKind','audioAge','frontNode','frontCamera','cabinNode','audioNode'])$(id).textContent='—';$('attentionBar').style.width=$('drowsyBar').style.width='0';$('forward').textContent=$('yawn').textContent='수신 대기';}
function render(s){const snap=s.snapshot||{},d=snap.driver,a=snap.audio,now=Date.now(),fresh=now-Number(s.updated_time_ns)/1e6<1500; if(!fresh){offline('데이터 지연');return;}
 $('previewLabel').hidden=!s.preview;
 renderRisk(s.risk_model, now);
 $('connection').textContent=d&&a?'운전자·음향 수신 중':d?'운전자 수신 · 음향 대기':a?'음향 수신 · 운전자 대기':'입력 데이터 대기';$('samples').textContent=(s.received_samples||{}).driver_state||0;$('health').textContent=(snap.degraded_reasons||[]).includes('heartbeat_missing:jetson-cabin')?'Jetson 응답 없음':d?'연결됨':'상태 미수신';
 if(s.clock_skew_suspected)$('health').textContent='장치 시각 확인 필요';
 const nodes=s.nodes||{};for(const [id,name] of [['frontNode','jetson-front'],['cabinNode','jetson-cabin.link'],['audioNode','jetson-cabin.audio']]){const n=nodes[name];$(id).textContent=n&&n.connected?'통신 연결됨':'응답 없음';}$('frontCamera').textContent=s.front_camera_enabled?'활성':'미연결 · 입력 제외';
 const audioNames={none:'사이렌 미검출',siren:'사이렌 후보 · 시험 판정',horn:'경적 감지',other:'기타 음향'};
 $('audioKind').textContent=a?(audioNames[a.kind]||a.kind):'수신 없음 · 입력 확인 필요';$('audioAge').textContent=a?Math.max(0,now-Number(a.header.capture_time_ns)/1e6).toFixed(0)+' ms':'—';$('audioSamples').textContent=(s.received_samples||{}).audio_event||0;
 $('audioMode').textContent=(s.audio_input||{}).alerts_enabled?'음향 경고 활성 · 경적 비활성 · 시험 입력':'음향 시험 표시 모드 · 오탐 확인으로 자동 음성 경고 보류 · 경적 비활성';
 const t=s.speech||{};$('speech').textContent=t.error?'출력 장치 확인 필요':t.enabled?`${t.voice==='F1'?'여성 음성':'음성'} 활성 · ${t.completed||0}회 재생 완료`:'꺼짐';
 const command=s.command&&Number(s.command.expires_at_ns)/1e6>now?s.command:null;
 $('hero').className='hero'+(command?' '+({1:'info',2:'warn',3:'critical'}[command.severity]||''):'');
 $('level').textContent=command?({1:'INFO · 운전자 상태 안내',2:'WARNING · 주의',3:'CRITICAL · 긴급'}[command.severity]):'운전자 상태 모니터링';
 $('message').textContent=command?command.display_text:!d?'운전자 데이터 수신 대기':!d.face_detected?'운전자 행동이 검출되지 않았습니다':'운전자 상태를 확인하고 있습니다';
 $('description').textContent=command?command.tts_text:!d?'카메라·Jetson 연결을 확인해 주세요':!d.face_detected?'검출 없음은 정상 판정이 아닙니다':'차량·도로 정보는 미연결 상태입니다';
 $('attention').textContent=d&&d.face_detected?Math.round(d.attention_score*100)+' / 100':'—';$('drowsy').textContent=d&&d.face_detected?Math.round(d.drowsiness_score*100)+' / 100':'—';$('forward').textContent=d&&d.face_detected?(d.looking_forward?'전방 주의 추정':'주의 분산 추정'):'행동 미검출';$('phone').textContent=d&&d.face_detected?(d.phone_detected?'휴대폰 감지':'휴대폰 미검출'):'—';$('yawn').textContent=d&&d.face_detected?(d.yawning?'하품 감지':'하품 미검출'):'행동 미검출';
 $('attentionBar').style.width=d&&d.face_detected?d.attention_score*100+'%':'0';$('drowsyBar').style.width=d&&d.face_detected?d.drowsiness_score*100+'%':'0';$('presence').textContent=d?(d.face_detected?'검출됨':'미검출'):'수신 없음';$('age').textContent=d?Math.max(0,now-Number(d.header.capture_time_ns)/1e6).toFixed(0)+' ms':'—';$('inference').textContent=d?d.inference_ms.toFixed(1)+' ms':'—';
 const names={driver_phone_observation:'휴대폰 사용 감지',driver_drowsy_observation:'졸음 징후 감지',driver_attention_observation:'주의 분산 감지',driver_inattention:'전방 주의 필요',emergency_vehicle:'사이렌 감지 · 긴급차량 주의'};
 $('events').replaceChildren(...(s.events||[]).slice(0,5).map(e=>{const row=document.createElement('div');row.textContent=new Date(Number(e.header.capture_time_ns)/1e6).toLocaleTimeString('ko-KR')+' · '+(names[e.code]||e.code);return row}));
 if(!(s.events||[]).length)$('events').textContent='아직 안내가 없습니다.';$('updated').textContent='최근 갱신 '+new Date(Number(s.updated_time_ns)/1e6).toLocaleTimeString('ko-KR');
}
async function poll(){try{const response=await fetch('/api/state',{cache:'no-store',signal:AbortSignal.timeout(2000)});if(!response.ok)throw Error();const s=await response.json();render(s);lastGood=Date.now();}catch(e){offline('Pi 연결 끊김');}finally{setTimeout(poll,250)}}poll();
