# 분할 창 이동 단축키와 보기 이름

목표: 새 세션·기존 세션의 탭을 ‘터미널 / 채팅’으로 표시하고, Mac·iPad 키보드의 Command+Option+방향키가 실제 입력한 분할 창을 기준으로 포커스를 이동한다.

확인한 원인:
- `workspace.shortcut`은 Control+Command를 필수로 요구한다. Command+Option+방향키는 처리되지 않아 입력창/CLI/브라우저에 남는다. 기존 설정 조합을 보존하면서 사용자가 요청한 방향키 별칭을 제공한다.
- iframe은 원래 창 ID를 `handleKey`로 전달하지만, 이 함수의 이웃 계산은 해당 ID 대신 마지막 active 전역 값만 사용한다. 포커스 알림보다 키가 먼저 도착하면 잘못된 방향을 기준으로 아무 동작도 하지 않을 수 있다.

파일 매핑:
- `web/workspace.js`: 네 방향의 Command+Option 별칭, iPad 방향키 이름 정규화, 키 이벤트를 보낸 pane ID 기준 이웃 계산. 기존 Control+Command·설정의 3키 조합·한글 조합·dialog 차단·Option 단독 편집 동작·포커스 보존 규칙 유지.
- `web/index.html`: 단축키 도움말에 Command+Option+방향키 추가. 숫자/Enter와 기존 설정은 유지.
- `web/app.js`: 공통 탭 생성기의 표시 이름·접근성 이름만 변경. 저장된 mode 키와 복원 경로는 유지.
- `tests/test_workspace.cjs`: 현재 실패하는 조합과 stale active 재현, 분할 iframe의 textarea/xterm 대상 capture 전달을 검증.
- `tests/test_frontend.cjs`: 기존 탭 이름 검증을 새 명칭으로 갱신.
- `README.md`, `docs/limitations.md`, `scripts/public-files.json`, 이 계획: 새 명칭과 단축키 지원 범위 기록.
- 서버/PTY/전송/권한 설정은 수정하지 않는다. 권한 보조 앱은 별도 설계·실기 검증 대상이다.

순서·검증:
1. 기본 shortcut 모델과 실제 sender pane 불일치 fixture에서 실패를 확인한다.
2. 방향키만 별칭으로 추가하고 이벤트 source pane을 사용한다. 글자/일반 Option+방향키·Cmd+Enter·IME·열린 dialog는 기존 경로에 남긴다.
3. 관련 workspace 테스트, 전체 JavaScript, 공개 파일 감사·diff 검사와 셀프 리뷰를 수행한다. Python·전송 경로는 불변이므로 전체 Python은 재실행하지 않는다.
4. 기존 변경을 보존해 한 번 배포하고 새 앱/탭/단축키 자산과 기존 터미널·설정·전체 관리 파일 해시를 확인한다. 실제 iPad/Mac 하드웨어 키 수신은 자동 fixture와 구분한다.
