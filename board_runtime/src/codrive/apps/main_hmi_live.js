/* Live adapter for the team's main HMI. No demo inputs or browser speech synthesis. */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const esc = value => String(value).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
  const colors = { UNKNOWN: "#9aaeb9", NORMAL: "#36d39a", CAUTION: "#f5c84b", WARNING: "#ff933d", EMERGENCY: "#ff4d4d" };
  const ranks = { UNKNOWN: 0, NORMAL: 0, CAUTION: 1, WARNING: 2, EMERGENCY: 3 };
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const fresh = (sample, now, ttl) => {
    const stamp = sample?.header?.capture_time_ns;
    return finite(stamp) && now - stamp >= -5e6 && now - stamp <= ttl * 1e6;
  };

  // Pure projection is also exercised by browser tests; TTLs use the Pi's clock.
  function project(state, now) {
    const live = finite(state?.updated_time_ns) && now - state.updated_time_ns >= -5e6 && now - state.updated_time_ns < 1.5e9;
    const snapshot = live ? state.snapshot || {} : {};
    const driver = fresh(snapshot.driver, now, 400) ? snapshot.driver : null;
    // Vehicle validity is decided by the Pi receiver; do not add a shorter UI timeout.
    const vehicle = snapshot.vehicle?.can_valid === true ? snapshot.vehicle : null;
    const audio = fresh(snapshot.audio, now, 1000) ? snapshot.audio : null;
    const environment = fresh(snapshot.environment, now, 3000) ? snapshot.environment : null;
    const weatherValid = environment?.api_valid === true &&
      (!finite(environment.weather_valid_until_ns) || now < environment.weather_valid_until_ns);
    const command = live && state.command?.expires_at_ns > now ? state.command : null;
    const behaviors = {
      awake: ["NORMAL", "정상", "깨어 있음 · 전방 주시"],
      distracted: ["CAUTION", "주의 분산", "전방을 주시하세요"],
      drowsy: ["WARNING", "졸음 감지", "안전한 곳에서 쉬어가세요"],
      head_drop: ["WARNING", "고개 떨굼 감지", "안전한 곳에 정차하세요"],
      phone: ["CAUTION", "휴대폰 사용 감지", "휴대폰을 내려놓으세요"]
    };
    // Older publishers omit behavior; an explicit null means no accepted detection.
    const behavior = driver && !("behavior" in driver) && driver.face_detected ?
      (driver.phone_detected ? "phone" : driver.drowsiness_score >= .65 ? "drowsy" :
        !driver.looking_forward ? "distracted" : "awake") : driver?.behavior;
    const current = behaviors[behavior];
    let level = current?.[0] || "UNKNOWN";
    let title = current?.[1] || "상태 수신 대기";
    let action = current?.[2] || (!live ? "Pi 연결을 확인하세요" :
      driver ? "운전자 행동 미검출" : "내부 카메라 상태 미수신");
    let reason = current ? `내부 카메라 · ${current[1]}` : action;
    const ruleNames = {road_user_attention:"주변 객체", stop_sign_attention:"정지 표지판",
      red_light_approach:"신호등", yellow_light_approach:"신호등", emergency_vehicle:"사이렌", horn_alert:"경적",
      full_accelerator:"가속페달", rapid_pedal:"가속페달 조작", rapid_acceleration:"급가속"};
    const ruleName = ruleNames[command?.risk_code] || "운전자 상태";
    const ruleLevel = command ? ({1: "CAUTION", 2: "WARNING", 3: "EMERGENCY"})[command.severity] : null;
    // The Pi has already selected priority and retained the spoken warning.
    const ruleWins = !!ruleLevel;
    if (ruleWins) {
      level = ruleLevel;
      title = level === "EMERGENCY" ? "긴급 경고" : level === "CAUTION" ? "주의" : "경고";
      action = command.display_text || "주변을 확인하세요";
      reason = `위험 안내 · ${ruleName}`;
    }
    const driverLabel = current?.[1] || (driver ? "운전자 행동 미검출" : "내부 카메라 상태 미수신");
    const nodes = Object.values(state?.nodes || {});
    const connected = live && nodes.length >= 4 && nodes.every(node => node.connected === true &&
      finite(node.age_ms) && node.age_ms + (now - state.updated_time_ns) / 1e6 < 1500);
    const reasonLabel = ruleWins ? `위험 안내 · ${ruleName}` : "내부 카메라 · 운전자 상태";
    const prediction=state?.risk_model;
    const modelText=live && prediction?.status === "ok" &&
      finite(prediction.valid_until_ns) && now < prediction.valid_until_ns &&
      typeof prediction.warning_message === "string" ? prediction.warning_message : "";
    return {modelText, modelRank: ({LOW:1,MEDIUM:2,HIGH:3})[prediction?.xgboost_risk_level] || 0, guide: live && vehicle && state?.driving_corridor?.status === "active" ? state.driving_corridor.guide : null, live, driver, driverLabel, vehicle, audio, environment, weatherValid, audioAlerts: state?.audio_input?.alerts_enabled === true, level, title, action, reason, reasonLabel, connected,
      playing: live && state?.speech?.playing === true &&
        state.speech.playing_event_id === command?.event_id};
  }

  function status(id, icon, text, changed = false) {
    const node = $(id);
    node.classList.toggle("changed", changed);
    node.innerHTML = `<span class="status-icon">${icon}</span><span>${esc(text)}</span>`;
  }
  let previousAssist = null;
  const cameras = Object.fromEntries([
    ["front", "frontCamera", "외부 카메라"], ["cabin", "cabinCamera", "내부 카메라"]
  ].map(([key, id, label]) => [key, {
    key, image:$(id), label, url:null, healthy:false, checkedAt:0, checking:false, retryAt:0, generation:0
  }]));
  let mainCamera = "front";
  try { if (localStorage.getItem("codrive.mainCamera") === "cabin") mainCamera = "cabin"; } catch (_) {}
  async function checkCamera(camera) {
    camera.checking = true;
    const {url, generation} = camera;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 1200);
    try {
      const healthURL = new URL(url);
      healthURL.pathname = "/health";
      healthURL.search = "";
      const response = await fetch(healthURL, {cache:"no-store", signal:controller.signal});
      if (!response.ok) throw Error("Camera HTTP error");
      const health = await response.json();
      if (camera.generation === generation) camera.healthy = health.status === "ok" && health.sequence > 0 &&
        finite(health.frame_age_ms) && health.frame_age_ms >= 0 && health.frame_age_ms < 1500;
    } catch (_) {
      if (camera.generation === generation) camera.healthy = false;
    } finally {
      clearTimeout(timeout);
      if (camera.generation === generation) {
        camera.checking = false;
        camera.checkedAt = performance.now();
      }
    }
  }
  function updateCamera(camera, url) {
    try {
      const parsed = new URL(url || "", window.location.href);
      url = url && ["http:", "https:"].includes(parsed.protocol) ? parsed.href : null;
    } catch (_) { url = null; }
    if (url !== camera.url) {
      camera.generation += 1;
      camera.url = url;
      camera.healthy = false;
      camera.checking = false;
      camera.checkedAt = -Infinity;
      camera.image.removeAttribute("src");
      if (url) { camera.image.src = url; camera.retryAt = performance.now(); }
    }
    const now = performance.now();
    if (url && !camera.checking && now - camera.checkedAt > 500) checkCamera(camera);
    camera.live = !!url && camera.healthy && now - camera.checkedAt < 1500 && camera.image.naturalWidth > 0;
    if (url && !camera.live && now - camera.retryAt > 3000) {
      const retry = new URL(url);
      retry.searchParams.set("retry", Math.floor(now));
      camera.image.src = retry.href;
      camera.retryAt = now;
    }
  }
  function renderCameras(frontURL, cabinURL) {
    updateCamera(cameras.front, frontURL);
    updateCamera(cameras.cabin, cabinURL);
    const main = cameras[mainCamera];
    const side = cameras[mainCamera === "front" ? "cabin" : "front"];
    const panel = $("assistPanel"), sideFrame = $("sideCameraFrame");
    // Move the existing image nodes: swapping does not replace either stream URL.
    if (main.image.parentElement !== panel) panel.prepend(main.image);
    if (side.image.parentElement !== sideFrame) sideFrame.prepend(side.image);
    main.image.className = "camera-feed";
    side.image.className = "side-camera-feed";
    panel.classList.toggle("camera-live", main.live);
    panel.classList.toggle("camera-loading", !main.live && !!(frontURL || cabinURL));
    $("cameraStateText").textContent = `${main.label} 영상 연결 대기`;
    $("mainCameraLabel").textContent = main.label;
    $("sideCameraLabel").textContent = side.label;
    sideFrame.classList.toggle("is-live", side.live);
    $("sideCameraMessage").textContent = `${side.label} ${side.url ? "연결 대기" : "미설정"}`;
    $("swapCameras").setAttribute("aria-pressed", String(mainCamera === "cabin"));
    const received = [cameras.front.live ? "외부" : null, cameras.cabin.live ? "내부" : null].filter(Boolean);
    status("laneStatus", "📷", received.length ? `${received.join("·")} 영상 수신` :
      frontURL || cabinURL ? "카메라 영상 대기" : "카메라 미설정", !received.length);
  }
  function guideSvg(guide) {
    const point = p => Array.isArray(p) && p.length === 2 && p.every(finite);
    if (!guide || !finite(guide.width) || !finite(guide.height)) return "";
    const left = (guide.left || []).filter(point), right = (guide.right || []).filter(point);
    if (left.length < 2 || right.length < 2) return "";
    const path = values => values.map((p,i) => `${i ? "L" : "M"}${p[0]},${p[1]}`).join(" ");
    const sides = [left,right].map(values => `<path class="guide-outline" d="${path(values)}"/><path class="guide-edge" d="${path(values)}"/>`).join("");
    const polygon = [...left,...right.slice().reverse()].map(p=>p.join(",")).join(" ");
    const centre = (guide.center || []).filter(point);
    const bars = (guide.rungs || []).filter(r=>point(r.left)&&point(r.right)).map(r=>`
      <line class="guide-rung" x1="${r.left[0]}" y1="${r.left[1]}" x2="${r.right[0]}" y2="${r.right[1]}"/>
      <text class="guide-label" x="${r.right[0]+9}" y="${r.right[1]+5}">${esc(r.distance_m)}m</text>`).join("");
    const targets = (guide.targets || []).filter(t=>finite(t.ttc_s)&&point(t.point)&&Array.isArray(t.bbox)&&t.bbox.length===4&&t.bbox.every(finite)).slice(0,3).map(t=>{
      const [x1,y1,x2,y2]=t.bbox;
      const colour=t.ttc_s<1.5?"#ff665b":t.ttc_s<3?"#ffc05d":"#d9ed7c";
      const names={person:"보행자",pedestrian:"보행자",car:"차량",vehicle:"차량",truck:"트럭",bus:"버스",bicycle:"자전거",motorcycle:"이륜차"};
      const label=`${names[t.class]||"물체"} · ${t.ttc_s.toFixed(1)}초`;
      const width=Math.max(135,label.length*12), y=Math.max(24,y1-10);
      return `<g><rect x="${x1}" y="${y1}" width="${Math.max(1,x2-x1)}" height="${Math.max(1,y2-y1)}" rx="5" fill="none" stroke="${colour}" stroke-width="2.5" vector-effect="non-scaling-stroke"/>
        <rect x="${x1}" y="${y-23}" width="${width}" height="29" rx="6" fill="#08151de8"/>
        <text class="guide-target-label" x="${x1+8}" y="${y-2}" style="fill:${colour}">${esc(label)}</text>
        <circle cx="${t.point[0]}" cy="${t.point[1]}" r="5" fill="${colour}"/>
        ${point(t.motion_end)?`<path d="M${t.point.join(",")} L${t.motion_end.join(",")}" fill="none" stroke="#9bd4ff" stroke-width="3" stroke-dasharray="5 4" marker-end="url(#targetMotionArrow)" vector-effect="non-scaling-stroke"/>`:""}</g>`;
    }).join("");
    return `<defs><linearGradient id="guideDepth" x1="0" y1="${guide.height*.52}" x2="0" y2="${guide.height}" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#72f2b6"/><stop offset=".65" stop-color="#c6f294"/><stop offset="1" stop-color="#ffc05d"/></linearGradient><marker id="targetMotionArrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 Z" fill="#9bd4ff"/></marker></defs>
      <polygon points="${polygon}" fill="#64e9ab" fill-opacity=".055"/>${sides}${bars}
      ${centre.length>1?`<path d="${path(centre)}" fill="none" stroke="white" stroke-opacity=".32" stroke-width="1.2" stroke-dasharray="7 9" vector-effect="non-scaling-stroke"/>`:""}${targets}`;
  }
  let previousGuide = "";
  function renderGuide(view) {
    const node=$("drivingGuide");
    const guide=view.guide;
    const visible=mainCamera==="front" && cameras.front.live && guide;
    const body=visible?guideSvg(guide):"";
    if (body!==previousGuide) { node.innerHTML=body; previousGuide=body; }
    node.classList.toggle("is-visible",!!body);
    node.setAttribute("aria-hidden",String(!body));
    if (body) node.setAttribute("viewBox",`0 0 ${guide.width} ${guide.height}`);
  }
  const explanationState={text:"",rank:0,shownAt:0,lastValid:0,candidate:"",candidateAt:0};
  function renderExplanation(view, now) {
    const box=$("xgboostExplanation");
    if (!box)return;
    const s=explanationState;
    if (!view.live) {
      s.text="";s.rank=0;s.candidate="";box.hidden=true;return;
    }
    box.hidden=false;
    if (view.modelText) {
      s.lastValid=now;
      if (s.candidate!==view.modelText) {s.candidate=view.modelText;s.candidateAt=now;}
      // Escalation is immediate. Other wording changes must persist and remain readable.
      if (!s.text || view.modelRank>s.rank ||
          (now-s.shownAt>=2000 && (now-s.candidateAt>=500 || now-s.shownAt>=4000))) {
        if (s.text!==view.modelText) {s.text=view.modelText;s.rank=view.modelRank;s.shownAt=now;}
      }
    } else if (now-s.lastValid>1500) {s.candidate="";}
    const recent=s.text && now-s.lastValid<=1500;
    const text=recent ? (s.text.trim()==="NORMAL" ? "현재 입력 기준 추가 위험 안내가 없습니다." : s.text) : "현재 설명을 갱신할 수 없습니다.";
    const label="XGBoost 설명"+(recent && !view.modelText ? " · 갱신 중" : "");
    if ($("xgboostText").textContent!==text)$("xgboostText").textContent=text;
    if (box.querySelector("strong").textContent!==label)box.querySelector("strong").textContent=label;
  }
  function render(view) {
    renderExplanation(view,performance.now());
    $("hmiRoot").style.setProperty("--risk-color", colors[view.level]);
    const panel = $("assistPanel");
    panel.className = `assist ${view.level === "UNKNOWN" ? "normal" : view.level.toLowerCase()}`;
    const body = ranks[view.level] > 0 ? `
      <div class="warning-toast" role="status" aria-live="polite" aria-atomic="true">
        <span class="warning-toast-level">${esc(view.title)}</span>
        <p class="warning-toast-action">${esc(view.action)}</p>
        ${view.playing ? '<span class="warning-toast-audio" aria-label="음성 안내 중">🔊</span>' : ''}
      </div>` : "";
    if (body !== previousAssist) {
      $("assistContent").innerHTML = body;
      previousAssist = body;
    }
    $("assistContent").title = view.reason;
    const vehicle = view.vehicle;
    $("speedValue").textContent = finite(vehicle?.speed_mps) ? Math.round(vehicle.speed_mps * 3.6) : "0";
    $("steeringValue").textContent = finite(vehicle?.steering_wheel_deg) ? `${Math.round(vehicle.steering_wheel_deg)}°` : "0°";
    $("brakeValue").textContent = typeof vehicle?.brake_pressed === "boolean" ? (vehicle.brake_pressed ? "ON" : "OFF") : "OFF";
    const indicator = vehicle?.turn_signal;
    $("turnSignalValue").textContent = indicator === "left" ? "◀ 왼쪽" : indicator === "right" ? "오른쪽 ▶" : "OFF";
    $("turnSignalValue").classList.toggle("turn-indicator-on", ["left", "right"].includes(indicator));
    $("acceleratorValue").textContent = finite(vehicle?.accelerator_pct) ? `${Math.round(vehicle.accelerator_pct)}%` : "0%";
    $("acceleratorBar").style.width = `${finite(vehicle?.accelerator_pct) ? Math.max(0, Math.min(100, vehicle.accelerator_pct)) : 0}%`;
    $("fallbackBox").classList.toggle("visible", !vehicle);
    const env = view.environment;
    const weatherLabels = {clear:"맑음", cloudy:"흐림", rain:"비", snow:"눈", fog:"안개", thunder:"뇌우", unknown:"미수신"};
    $("weatherIcon").textContent = view.weatherValid ? "☁" : "◌";
    const daylight = env?.daylight_valid === true ? (env.is_night ? "야간" : "주간") : "주야간 미수신";
    $("weatherText").textContent = view.weatherValid ?
      `${latest?.scenario_context?.weather_override ? "시연 임의값 · " : ""}${weatherLabels[env.weather] || "미수신"} · ${daylight}${finite(env.temperature_c) ? ` · ${env.temperature_c.toFixed(1)}°C` : ""}` : `날씨 미수신 · ${daylight}`;
    $("systemState").classList.toggle("degraded", true);
    $("systemText").textContent = view.live ? (latest?.test_mode === "real-vehicle" ? "실차 수신 시험 · 거리 보정 전" : latest?.test_mode === "carla-screen" ? (latest?.driving_corridor?.status === "disabled" ? "모니터 촬영 · 거리 보정 전" : "모니터 촬영 · 화면 보정 적용") : "LIVE · 입력 제한") : "PI 연결 대기";
    renderCameras(view.live ? latest?.camera_preview?.stream_url || null : null,
      view.live ? latest?.camera_preview?.cabin_stream_url || null : null);
    renderGuide(view);
    status("trafficStatus", "👤", view.driverLabel, !view.driver?.face_detected);
    status("audioStatus", "🔊", view.audio ? `${view.audio.kind === "siren" ? "사이렌" : view.audio.kind === "horn" ? "경적" : "음향 수신"}${["left","right"].includes(view.audio.direction) ? ` · ${view.audio.direction === "left" ? "왼쪽" : "오른쪽"}` : ""}${view.audioAlerts ? "" : " · 자동 경고 꺼짐"}` : "음향 수신 대기", !view.audio);
    status("connectionStatus", "📡", view.connected ? "보드 연결 정상" : "보드 연결 확인", !view.connected);
    $("clock").textContent = new Date().toLocaleTimeString("ko-KR", {hour12:false, hour:"2-digit", minute:"2-digit", second:"2-digit"});
    $("hmiRoot").classList.add("live");
  }

  let latest = null, receivedAt = 0, piTime = 0;
  function tick() {
    const age = performance.now() - receivedAt;
    render(project(age < 1500 ? latest : null, piTime + age * 1e6));
  }
  let nextLatencyReport=0, lastLatencyEvent=null;
  function reportRender(state, pollMs) {
    if (!state.latency_probe || !['127.0.0.1','localhost'].includes(location.hostname)) return;
    const event=state.command?.event_id || null;
    if (performance.now()<nextLatencyReport && event===lastLatencyEvent) return;
    nextLatencyReport=performance.now()+1000; lastLatencyEvent=event;
    const keys=(state.snapshot?.external || []).map(e=>`${e.header.source_id}:${e.header.boot_id}:${e.header.sequence}`);
    if (state.snapshot?.driver) {const h=state.snapshot.driver.header; keys.push(`${h.source_id}:${h.boot_id}:${h.sequence}`);}
    requestAnimationFrame(()=>requestAnimationFrame(()=>{
      fetch('/api/latency-render',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({event_id:event, inputs:keys, snapshot_time_ns:state.updated_time_ns,
          browser_wall_ns:Date.now()*1e6,poll_s:pollMs/1000, endpoint:'two_raf_opportunity_not_physical_display'})}).catch(()=>{});
    }));
  }
  async function poll() {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 1000);
    const started = performance.now();
    try {
      const response = await fetch("/api/hmi", {cache:"no-store", signal:controller.signal});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const state = await response.json();
      if (!finite(state.server_time_ns)) throw new Error("Missing Pi timestamp");
      latest = state;
      receivedAt = performance.now();
      piTime = state.server_time_ns + (receivedAt - started) * 1e6;
      reportRender(state, receivedAt-started);
    } catch (_) {
      // Watchdog expires cached state even if the network request hangs or fails.
    } finally {
      clearTimeout(timeout);
      tick();
      setTimeout(poll, 100);
    }
  }
  $("testControls").remove();
  $("swapCameras").addEventListener("click", () => {
    mainCamera = mainCamera === "front" ? "cabin" : "front";
    try { localStorage.setItem("codrive.mainCamera", mainCamera); } catch (_) {}
    tick();
  });
  window.CODRIVE_LIVE = {project, guideSvg};
  tick();
  setInterval(tick, 50);
  poll();
})();
