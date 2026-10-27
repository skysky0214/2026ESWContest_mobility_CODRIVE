# 새 랩탑에서 붙여 넣을 프롬프트

CODRIVE / ESW2026 개발을 다른 랩탑에서 이어간다.
현재 저장소의 integration/wired-hmi-20261021 브랜치를 기준으로
먼저 docs/HANDOFF_2026-10-21.md, docs/WIRED_DDS.md, docs/JETSON_CLOCK.md,
docs/DATA_CONTRACT.md를 읽고 코드·보드 서비스·시각·HMI 수신 상태를 확인해줘.

두 Jetson과 Pi의 유선 DDS·DMS/음향→HMI·부팅 자동 실행·1970년 시각 복원은 완료했다.
이미 설치한 보드 환경을 다시 구성하지 말고 기존 대화의 계정·주소·접속 정보를 사용해줘.
새 랩탑에서는 SSH와 HMI 터널을 새로 열어야 한다.

외부 카메라는 나중에 연결하고 CARLA/CAN은 별도 작업이다.
다음 후보는 origin/XGBoost의 15입력 모델을 현재 Pi 경로에 연결하고 최종 HMI/TTS를 만드는 것이다.
XGBoost는 LLM이 아니며 현재 아직 실시간 미연결이다. 음향 오탐 때문에 --audio-alerts는 보류한다.
미수신 값을 정상으로 대체하거나 실시간 domain 42에 합성 인지값을 넣지 마라.
우선 인수 상태를 짧게 보고하고, 다음 개발은 내가 지정하는 작업에 맞춰 진행해줘.
