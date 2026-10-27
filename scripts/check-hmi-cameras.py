"""Offline browser checks for two live MJPEG feeds; no DDS, camera device or TTS.

Run: PYTHONPATH=board_runtime/src .venv/bin/python scripts/check-hmi-cameras.py
Requires Playwright/Chromium, numpy and OpenCV in the test environment.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from playwright.sync_api import sync_playwright

from codrive.apps.pi import DashboardState, make_http_server
from codrive.transport.mjpeg import MjpegStreamConfig, MjpegStreamServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/codrive-hmi-cameras"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    state = DashboardState()
    front = MjpegStreamServer(MjpegStreamConfig(host="127.0.0.1", port=0))
    cabin = MjpegStreamServer(MjpegStreamConfig(host="127.0.0.1", port=0))
    servers = (front, cabin)
    for server in servers:
        server.start()
    jpeg = []
    for label, colour in [("FRONT TEST", (75, 65, 20)), ("CABIN TEST", (20, 65, 75))]:
        frame = np.full((360, 640, 3), colour, dtype=np.uint8)
        cv2.putText(frame, label, (65, 190), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (240, 240, 240), 3)
        ok, encoded = cv2.imencode(".jpg", frame)
        assert ok
        jpeg.append(encoded.tobytes())
    stop = threading.Event()
    enabled, critical = [True, True], [False]

    def update():
        while not stop.is_set():
            now = time.time_ns()
            for index, server in enumerate(servers):
                if enabled[index]:
                    server.publish_jpeg(jpeg[index])
            state.update({
                "updated_time_ns": now, "snapshot": {},
                "camera_preview": {"stream_url": front.stream_url, "cabin_stream_url": cabin.stream_url},
                "command": {"severity": 3, "expires_at_ns": now + 500_000_000,
                            "display_text": "긴급 경고 시험"} if critical[0] else None,
            })
            stop.wait(.05)

    updater = threading.Thread(target=update, daemon=True)
    updater.start()
    http = make_http_server("127.0.0.1", 0, state)
    runner = threading.Thread(target=http.serve_forever, daemon=True)
    runner.start()
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1920, "height": 1080})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{http.server_port}/", wait_until="domcontentloaded")
            page.wait_for_selector("#assistPanel.camera-live")
            page.wait_for_selector("#sideCameraFrame.is-live")
            assert page.locator("#assistPanel > #frontCamera").count() == 1
            assert page.locator("#sideCameraFrame > #cabinCamera").count() == 1
            mapping = page.evaluate("""() => {
              const now = 1e12;
              const make = behavior => ({updated_time_ns:now, snapshot:{driver:{
                header:{capture_time_ns:now}, face_detected:behavior !== null,
                behavior, looking_forward:behavior === 'awake', attention_score:.2,
                drowsiness_score:.9, phone_detected:behavior === 'phone'}},
                command:{severity:1, expires_at_ns:now+1e9, display_text:'old distraction'}});
              const expected = {awake:'정상', distracted:'주의 분산', drowsy:'졸음 감지',
                head_drop:'고개 떨굼 감지', phone:'휴대폰 사용 감지'};
              for (const [key,title] of Object.entries(expected)) {
                const view = window.CODRIVE_LIVE.project(make(key),now);
                if(view.title !== title) throw Error(key + ': ' + view.title);
              }
              if(window.CODRIVE_LIVE.project(make(null),now).title !== '상태 수신 대기') throw Error('missing');
              return true;
            }""")
            assert mapping
            assert page.locator(".safe-title").is_visible()
            assert page.locator(".safe-title").inner_text() == "상태 수신 대기"
            original_urls = [page.locator(f"#{key}Camera").get_attribute("src") for key in ("front", "cabin")]
            for width, height in [(1920, 1080), (1280, 1024), (1366, 768), (1280, 720)]:
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_timeout(100)
                side = page.locator("#sideCameraFrame").bounding_box()
                vehicle = page.locator(".vehicle").bounding_box()
                fallback = page.locator("#fallbackBox").bounding_box()
                assert side["x"] >= vehicle["x"] and side["y"] > fallback["y"] + fallback["height"]
                assert side["y"] + side["height"] <= vehicle["y"] + vehicle["height"]
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(args.output / f"default-{width}-{height}.png"))
            page.get_by_role("button", name="내부·외부 카메라 위치 전환").click()
            page.wait_for_selector("#assistPanel > #cabinCamera")
            assert page.locator("#sideCameraFrame > #frontCamera").count() == 1
            assert [page.locator(f"#{key}Camera").get_attribute("src") for key in ("front", "cabin")] == original_urls
            assert page.locator("#mainCameraLabel").inner_text() == "내부 카메라"
            page.screenshot(path=str(args.output / "swapped.png"))
            page.reload(wait_until="domcontentloaded")
            page.wait_for_selector("#assistPanel > #cabinCamera")
            page.wait_for_selector("#assistPanel.camera-live")
            # Stop cabin frame production. Only its panel may become unavailable.
            enabled[1] = False
            page.wait_for_selector("#assistPanel.camera-loading", timeout=6000)
            assert page.locator("#sideCameraFrame.is-live").count() == 1
            assert "내부 카메라" in page.locator("#cameraStateText").inner_text()
            enabled[1] = True
            page.wait_for_selector("#assistPanel.camera-live", timeout=6000)
            critical[0] = True
            page.wait_for_selector("#assistPanel.emergency")
            page.locator("#swapCameras").focus()
            page.keyboard.press("Enter")
            page.wait_for_selector("#assistPanel > #frontCamera")
            assert "긴급 경고 시험" in page.locator("#assistContent").inner_text()
            assert "emergency" in page.locator("#assistPanel").get_attribute("class")
            assert not errors, errors
            browser.close()
        result = {"default_cabin_in_right_bottom": True, "swap_preserves_stream_urls": True,
                  "preference_survives_reload": True, "independent_stale_and_recovery": True,
                  "critical_warning_survives_swap": True, "keyboard_accessible": True,
                  "viewports": 4, "page_errors": errors}
        (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    finally:
        stop.set()
        updater.join()
        http.shutdown()
        http.server_close()
        runner.join()
        for server in servers:
            server.close()


if __name__ == "__main__":
    main()
