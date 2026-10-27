# 한국어 여성 TTS · Supertonic 3 F1

2026-10-26 판단 코어 복구: 기존 행동 안내 문장을 복원하고 eSpeak 대체 합성을 제거했다. 현재 Pi 배포 대상은 `ESW2026-can-v3`이며 F1 캐시로만 출력한다. 아래 병합 기록의 문구 개수·fallback 설명은 복구 이전 상태다.

## ESW2026-latest 반영 · 2026-10-26

여성 음성이 실제 적용됐던 `ESW2026-front-demo` (`40686a4`)의 F1 WAV 14개를 가져와
latest의 기존 짧은 안내 11개와 병합했다. 현재 `TEMPLATES`와 `EXTRA_TEMPLATES`에 필요한
문구 중 빠진 11개는 같은 검증된 Supertonic 3 모델·speaker_id 0·8 steps·speed 1.0으로
노트북에서 미리 생성했다. 준비된 WAV는 총 28개이며 현재 필수 문구 22개를 모두 포함한다.
추가분은 좌우 사이렌·경적, 급가속·급격한 페달 조작, 고개 떨굼, 정지 표지판 등의 안내다.

현재 알려진 안내는 모두 F1 WAV로 재생되며 실시간 eSpeak 대체 합성을 사용하지 않는다.
미등록 동적 문구에 대한 기존 eSpeak fallback 코드는 유지했다. 새 문구를 추가할 때는
여성 음성 캐시도 갱신해야 한다. 실제 문구별 F1 재생과 fallback 미사용을
`board_runtime/tests/test_female_voice_coverage.py`에서 확인한다.

파일별 SHA256·원본 모델 해시·가져온 워크스페이스·추가 생성 문구는 음성 manifest에 기록했다.
이번 변경 대상은 로컬 `ESW2026-latest`이며 실행 중인 Pi 서비스는 재배포하거나 재시작하지 않았다.

아래는 최초 적용 시점의 기록이다.

2026-10-21 Pi에 적용했다. 기존 espeak-ng 한국어 합성을 Supertonic 3 여성 F1 음성으로
교체했다. 사용자가 Pi 모니터에서 시험 문장이 들린다고 확인했다.

## 실행 경로

- 실제 Pi 코드: `/home/rp/ESW2026-integration`
- Pi HMI: 모니터의 Firefox에서 `http://127.0.0.1:8080/`
- 준비한 음성: `artifacts/tts/supertonic-ko-f1/`
- 실행 서비스: 기존 `codrive-pi`; XGBoost 가상환경과 drop-in은 유지
- 복구: `/home/rp/codrive-backups/tts-female-20261021.ryv45rc1/rollback.sh`

Supertonic 3 INT8 / sherpa-onnx 1.13.8 / speaker_id 0(F1) / language ko / 8 steps /
speed 1.0으로 11개 문구를 미리 생성했다. 모델 출력은 실제 44.1 kHz mono PCM16 WAV다.
현재 안내 템플릿 9개와 기본 안내·출력 시험 문구를 포함한다. 파일별 SHA256·음성 길이·
생성 시간·모델 파일 해시는 cache의 manifest.json에 기록한다.

실행 중에는 모델 추론이나 인터넷 연결 없이 검증된 WAV를 메모리에서 읽어 paplay로
재생한다. 따라서 문장을 생성하는 시간 때문에 경고가 유효기간을 넘기는 문제를 피한다.
최초 적용 버전은 알 수 없는 문구를 다른 문구로 대체하지 않고 오류를 알렸다.
latest의 미등록 문구 fallback 정책과 캐시 범위는 위 갱신 기록을 따른다.

## 확인

```bash
cd /home/rp/ESW2026-integration
PYTHONPATH=board_runtime/src .venv-risk/bin/python -m codrive.speech verify
PYTHONPATH=board_runtime/src .venv-risk/bin/python -m codrive.speech play-test
```

시험 문장은 “코드라이브 여성 음성 안내입니다. 전방을 주시하세요.”다. 시험은 DDS에
합성 위험 이벤트를 발행하지 않고 로컬 음성 출력만 사용한다.

실제 출력은 `alsa_output.platform-107c706400.hdmi.hdmi-stereo`였고 음소거 해제·볼륨 40%를
확인했다. 출력 장치 이름을 코드에 고정하지 않으며 실행 시 기본 물리 sink를 선택한다.
API의 speech에 engine, voice, language, cache_ready, error, completed를 제공한다.
재생 명령 성공만으로 사람이 들었다고 간주하지 않고, 이번에는 사용자 청취 확인도 받았다.

만료된 큐 항목은 재생하지 않고 출력 장치 확인 후에도 유효기간을 다시 검사한다.
대기열은 최신 한 건으로 제한한다. 음향 자동 경고는 보류하고 XGBoost는 화면 표시 전용이다.
기존 실제 DMS 감지 안내의 문구·발화 간격은 유지했다.

## 다시 생성하기

생성 단계에만 sherpa-onnx가 필요하다. Pi 실행 서비스는 이 라이브러리를 가져오지 않는다.
기존 음성을 덮어쓰지 않도록 새 빈 cache-dir을 지정한다.

```bash
.venv-risk/bin/python -m pip install 'sherpa-onnx==1.13.8'
# 아래 공식 모델 URL에서 받아 압축을 풀어 model-dir에 지정한다.
PYTHONPATH=board_runtime/src .venv-risk/bin/python -m codrive.speech build \
  --model-dir artifacts/models/tts/sherpa-onnx-supertonic-3-tts-int8-2026-05-11 \
  --cache-dir artifacts/tts/supertonic-ko-f1-new
```

다운로드한 139 MB 모델은 Git에 포함하지 않는다. 실행에 필요한 준비된 WAV와 캐시 명세는
소스 배포에 포함한다. 원본 모델 README는 모델 라이선스를 OpenRAIL-M으로 명시하며,
번들의 LICENSE는 샘플 코드의 MIT 라이선스다. 두 항목을 혼동하지 않는다.

## 출처

- [공식 sherpa-onnx 한국어 Supertonic 3 안내](https://k2-fsa.github.io/sherpa/onnx/tts/all/Korean/supertonic-3-ko.html)
- [사용한 고정 모델 릴리스](https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/sherpa-onnx-supertonic-3-tts-int8-2026-05-11.tar.bz2)
- [speaker_id 생성 순서](https://github.com/k2-fsa/sherpa-onnx/blob/master/scripts/supertonic/generate_voices_bin.py)
- [원 제작사 모델 설명](https://huggingface.co/Supertone/supertonic-3)

문서 예제의 sample_rate와 Python extra map 변경 방식은 실제 배포 API와 차이가 있었다.
실제 모델의 sample_rate를 사용하며 `generation.extra = {"lang": "ko"}`로 지정한다.


## 반복 관찰 안내 수정 · 2026-10-21

기존 Pi는 같은 INFO 관찰도 10초마다 다시 발화했다. 짧은 검출이나 불안정한 상태가
다시 발화를 만들 수 있어 `ObservationSpeechGate`를 추가했다.

- 주의 분산·휴대폰·졸음 INFO는 신선한 행동 검출이 같은 조건으로 2초 지속된 후 발화한다.
- 동일 관찰은 연속 상태에서 한 번만 발화 요청한다. 다른 관찰과 번갈아 나와도 이미 읽은
  관찰은 다시 읽지 않는다.
- 신선한 전방 주시·주의도 0.65 이상 상태가 5초 이어져야 다음 발생에 재발화할 수 있다.
- 미검출·수신 끊김·오래된 입력은 정상 복귀로 취급하지 않는다.
- WARNING/CRITICAL의 우선순위·기존 반복 간격은 유지한다. 화면의 관찰 표시는 유지한다.

모델 클래스나 가중치, 검출 임계값을 바꾼 것이 아니라 INFO 음성 발화 정책을 수정한 것이다.
검증은 `board_runtime/tests/test_observation_speech.py`에 있다.


### 2026-10-27 collision speech preemption

LocalSpeech now owns the paplay subprocess. A higher-priority command terminates active playback before the bounded worker starts the next clip. WARNING/CRITICAL pedestrian_collision and object_collision outrank noncollision guidance, including CRITICAL lane_departure. Lower-priority incoming or queued commands cannot displace collision speech; equal-priority repeats do not interrupt active speech. Expired commands remain rejected. Interrupted clips are counted separately and are not marked completed or failed. Existing female cached clips and FSM/TTC selection are unchanged.

Validation: speech, observation-gate, and Pi display tests passed (28). Threaded regression covers actual worker cancellation through the subprocess interface, collision playback start, and rejection of subsequent lane guidance. Live audible scenario timing has not yet been measured.
