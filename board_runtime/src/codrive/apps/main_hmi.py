"""Serve the team's main HMI design with the integration runtime's live input.

main_hmi.html is based on the upstream team layout, with local live-display adjustments.
Its demo, camera, CAN and external weather scripts are replaced by our live adapter.
"""

from pathlib import Path


def document() -> bytes:
    source = Path(__file__).with_name("main_hmi.html").read_text()
    start = source.index("  <script>")
    end = source.index("  </script>", start) + len("  </script>")
    source = source[:start] + '<script src="/main_hmi_live.js"></script>' + source[end:]
    # Hide initial demo values until the live adapter has rendered unknown values.
    source = source.replace("</head>", """<style>
      .hmi { visibility: hidden; }
      .hmi.live { visibility: visible; }
      .assist.caution, .assist.warning, .assist.emergency { background: #071015; }
      .assist::before { display: none; animation: none; }
      .driving-guide { position: absolute; inset: 0; z-index: 2; width: 100%; height: 100%;
        display: none; pointer-events: none; overflow: hidden; }
      .driving-guide.is-visible { display: block; }
      .guide-outline { fill: none; stroke: #052e24; stroke-width: 7; stroke-linejoin: round;
        stroke-linecap: round; vector-effect: non-scaling-stroke; opacity: .75; }
      .guide-edge { fill: none; stroke: url(#guideDepth); stroke-width: 3;
        stroke-linecap: round; stroke-linejoin: round; vector-effect: non-scaling-stroke; }
      .guide-rung { stroke: #72f2b6; stroke-width: 2; opacity: .7; vector-effect: non-scaling-stroke; }
      .guide-label { font: 700 16px sans-serif; fill: white; paint-order: stroke;
        stroke: #06121b; stroke-width: 4px; stroke-linejoin: round; }
      .guide-target-label { font: 800 19px sans-serif; fill: white; }
      .assist-content { position: absolute; inset: 0; width: auto; height: auto;
        display: flex; align-items: flex-end; justify-content: center;
        padding: 0 clamp(16px, 2vw, 32px) clamp(18px, 3vh, 32px); }
      .warning-toast { display: flex; align-items: center; gap: 14px;
        max-width: min(100%, 780px); padding: 14px 21px; border-radius: 10px;
        border: 1px solid #ffffff24; border-left: 4px solid var(--risk-color);
        background: rgba(7, 16, 21, .9); box-shadow: 0 4px 16px #0004;
        color: #f1f5f7; text-align: left; }
      .warning-toast-level { flex: 0 0 auto; color: var(--risk-color);
        font-size: clamp(15px, 1.25vw, 19px); font-weight: 800; }
      .warning-toast-action { margin: 0; font-size: clamp(21px, 1.95vw, 30px);
        line-height: 1.4; font-weight: 700; overflow-wrap: anywhere; }
      .xgboost-explanation { position: relative; flex: 0 0 auto; height: clamp(160px,24vh,260px); overflow: auto; min-height: 0;
        width: 100%; padding: 14px 16px;
        border: 1px solid #ffffff24; border-radius: 10px;
        background: rgba(7,16,21,.9); color: #dce8ee; text-align: left;
        font-size: clamp(19px,1.5vw,24px); line-height: 1.5; }
      .xgboost-explanation[hidden] { display: none; }
      .xgboost-explanation strong { display: block; color: #9abacb; font-size: .85em; margin-bottom: 4px; }
      .xgboost-explanation p { margin: 0; max-height: min(24vh, 9em); overflow: auto; overflow-wrap: anywhere; }
      .warning-toast-audio { flex: 0 0 auto; font-size: 16px; }
      .turn-indicator-on { color: #5ce6a3; animation: indicatorBlink 1s step-end infinite; }
      @keyframes indicatorBlink { 50% { opacity: .3; } }
      #testControls { display: none; }
      .vehicle { min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: clamp(6px, .7vh, 10px);
        padding: clamp(12px, 1.5vw, 24px); }
      .vehicle-list { gap: clamp(6px, .8vh, 10px); }
      .speed { padding: 0 0 4px; }
      .speed-value { font-size: clamp(48px, 5vw, 80px); }
      .fallback { padding: 9px 12px; }
      .side-camera { flex-shrink: 0; margin-top: auto; min-width: 0; width: 100%; }
      .side-camera-header { display: flex; align-items: center; justify-content: space-between;
        gap: 6px; margin-bottom: 8px; }
      .side-camera-header h2 { margin: 0; color: #c8d6dc; font-size: clamp(12px, 1.1vw, 17px); }
      .camera-swap { min-height: 36px; padding: 6px 10px; border: 1px solid #43606f;
        border-radius: 6px; background: #14252f; color: #e7f0f3; cursor: pointer;
        font: inherit; font-size: clamp(11px, .9vw, 14px); white-space: nowrap; }
      .camera-swap:hover { background: #223d4b; }
      .camera-swap:focus-visible { outline: 2px solid #6bd7ff; outline-offset: 2px; }
      .side-camera-frame { position: relative; width: 100%; aspect-ratio: 16 / 9;
        overflow: hidden; border: 1px solid #304650; border-radius: 8px; background: #060e13; }
      .side-camera-feed { width: 100%; height: 100%; object-fit: contain; visibility: hidden; }
      .side-camera-frame.is-live .side-camera-feed { visibility: visible; }
      .side-camera-message { position: absolute; inset: 0; display: grid; place-items: center;
        padding: 12px; color: #91a3ab; font-size: clamp(12px, 1.1vw, 17px); text-align: center; }
      .side-camera-frame.is-live .side-camera-message { display: none; }
      .main-camera-label { position: absolute; z-index: 4; top: 12px; left: 30px;
        padding: 6px 10px; border-radius: 5px; background: #071015cc; color: #d9e6eb;
        font-size: clamp(12px, 1.1vw, 17px); pointer-events: none; }
    </style></head>""")
    source = source.replace('<div class="assist-content" id="assistContent"></div>',
                            '<svg id="drivingGuide" class="driving-guide" viewBox="0 0 1280 720" '
                            'preserveAspectRatio="xMidYMid slice" role="img" aria-label="조향에 따른 예상 주행 경로"></svg>'
                            '<div class="assist-content" id="assistContent"></div>')
    source = source.replace('<div class="assist-content" id="assistContent"></div>',
                            '<div class="assist-content" id="assistContent">운전자 상태 수신 대기</div>')
    source = source.replace('      </aside>', '''
        <section id="xgboostExplanation" class="xgboost-explanation" hidden aria-label="XGBoost 설명">
          <strong>XGBoost 설명</strong><p id="xgboostText"></p>
        </section>
        <section class="side-camera" aria-label="보조 카메라 영상">
          <div class="side-camera-header">
            <h2 id="sideCameraLabel">내부 카메라</h2>
            <button id="swapCameras" class="camera-swap" type="button"
              aria-label="내부·외부 카메라 위치 전환" aria-pressed="false">⇄ 위치 전환</button>
          </div>
          <div id="sideCameraFrame" class="side-camera-frame">
            <img id="cabinCamera" class="side-camera-feed" alt="내부 카메라 실시간 영상">
            <span id="sideCameraMessage" class="side-camera-message">내부 카메라 연결 대기</span>
          </div>
        </section>
      </aside>''')
    source = source.replace('<div class="camera-shade"',
                            '<span id="mainCameraLabel" class="main-camera-label">외부 카메라</span>'
                            '<div class="camera-shade"')
    return source.encode()
