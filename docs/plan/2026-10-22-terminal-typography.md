# 터미널 글자 가독성 개선

목표: Mac·iPad·Android에서 같은 한글/영문 코딩 글꼴과 안정적인 셀 간격으로 터미널을 읽을 수 있게 한다.

확인한 사실: xterm 6.0.0 기본 DOM renderer를 사용하므로 출력이 저해상도 이미지인 것은 아니다. 현재는 설치된 시스템 글꼴에만 의존하며 기본 14px·줄 높이 1.2다. 전역 grayscale antialiasing도 상속한다. 기기별 글꼴 fallback과 작은 글자·좁은 줄 간격이 읽기 불편의 원인이라는 가설로 개선한다. 실제 체감 선명도는 실기 확인과 구분한다.

파일 매핑:
- 추가: 공식 D2Coding 1.4.0의 Regular/Bold를 손실 없는 WOFF2 포장으로 내장한 `web/vendor/d2coding-regular.woff2`, `d2coding-bold.woff2`, 원본 라이선스 `d2coding-LICENSE`.
- 수정: `web/vendor/versions.json`, `THIRD_PARTY_NOTICES.md` — 공식 출처, 원본/배포 해시, 변환 도구와 라이선스 기록.
- 수정: `web/app.js`, `web/style.css`, `web/sw.js` — 터미널을 열 때 글꼴을 bounded load하고 설치 후 셀을 측정한다. 실패 시 기존 시스템 글꼴로 계속 동작한다. 기본 15px와 줄 간격 1.35, 실제 굵은 글꼴, 글자 대비를 적용하며 기존 명시적 크기·A±·핀치는 보존한다. 웹폰트는 첫 터미널 열기 때만 내려받고 버전 캐시를 사용한다.
- 수정: `server.py`, `scripts/check_public.py`, `scripts/public-files.json` — vendor WOFF2 경로만 서빙하고 MIME과 버전 해시를 제공한다. 감사는 내장한 두 파일의 정확한 SHA-256만 허용하며 임의 binary는 계속 거절한다.
- 수정: `tests/test_frontend.cjs`, `tests/test_server.py`, `tests/test_public_tree.py`, `docs/limitations.md` — 폰트 로딩 성공/실패/지연·화면 이동 경계, 정적 파일 MIME/캐시, 감사 위조 거절을 검증한다.
- 참조만: xterm vendor JS, fit addon, 터미널/세션 실행 관리. 삭제 없음.

순서와 검증:
1. 공식 글꼴과 라이선스를 내려받아 해시를 기록하고 WOFF2 변환 전후 문자·셀 폭이 동일한지 확인한다. 각 파일 2MiB 이하와 한글 2셀/영문 1셀 폭을 검사한다.
2. 폰트 로딩이 완료된 뒤 xterm을 측정하도록 연결한다. 네트워크 실패/timeout은 보드 입력이나 터미널 열기를 영구 차단하지 않으며 오래된 세션의 비동기 결과는 새 세션을 덮지 않는다.
3. 관련 JS/Python 검증, 공개 파일 검사, diff check 후 기존 파이프라인으로 한 번 배포한다. 운영의 폰트 MIME·해시·기존 터미널 보존을 확인한다. 실제 사용자 세션에 시험 입력은 보내지 않는다.

셀프 리뷰: 글자 해상도를 임의 배수로 확대하거나 CSS scale로 터미널을 늘리지 않는다. 코드/한글 폭을 유지하고 폰트 미설치 기기에서도 일관된 표시를 제공한다. 외부 폰트 CDN 런타임 의존성이나 새 GPU renderer는 추가하지 않는다.

추가 요청 — 상태 줄 겹침:
- 재현: 격리 tmux의 같은 ANSI 스트림을 xterm headless 6.0.0의 120×30/80×24로 각각 읽으면, 작은 잘못된 격자에서 상태 줄이 7개로 누적됐다. 생성 직후 tmux 기본 80×24→attach 120×30 변경도 native 출력에 잔여 줄을 만들었다. 실제 사용자 CLI가 모든 경우에 같은 원인인지는 구분한다.
- 확인된 경로: `TerminalManager.create/_attach/read/resize`와 `fitTerminal/startTerminalReader`. 처음 열린 브라우저는 과거 여러 크기의 ANSI 버퍼를 현재 크기로 재생하고, passive 창도 독립적으로 xterm을 fit하므로 서버와 줄바꿈이 달랐다. 재포커스 시 같은 크기에도 SIGWINCH를 반복했다.
- 변경: `terminal.py`는 생성과 attach 초기 크기를 120×30으로 통일한다. `read(fresh=True)`와 커서 만료는 현재 offset부터 tmux `refresh-client`로 화면을 다시 받고 서버 크기를 응답한다. `resize`는 실제 PTY 크기와 같으면 알림을 생략한다. macOS의 detached attach는 ioctl만으로 크기 변경을 받지 않음을 실측해 필요한 명시적 SIGWINCH는 유지한다.
- 변경: `server.py`는 첫 화면 재연결 플래그를 검증해 전달한다. `web/app.js`는 서버 크기를 출력 전에 적용하고, passive 창은 해당 크기를 유지한다. 크기 소유권을 가진 창만 제안한 크기를 서버에 보내며 새 출력의 크기가 확정될 때까지 로컬만 먼저 줄바꿈하지 않는다. 기록/초안/실행 입력과 무관한 화면 재그리기다.
- 검증: `tests/test_terminal.py`의 실제 격리 PTY에서 생성 크기·변경 크기·같은 크기 무신호·fresh/epoch 만료 재그리기·CLI 재실행 없음, `tests/test_frontend.cjs`의 출력 전 크기 적용·passive 로컬 fit 금지·포커스 창 크기 요청을 검사한다. headless 실측에서 화면 재연결 후 최신 상태 줄 1개와 tmux 캡처가 일치했다. 실사용 화면의 체감과 native CLI 자체의 중복 출력까지 해결됐다고 일반화하지 않는다.
- 추가로 확인한 원인: 출력 수신마다 `fitTerminal`이 180ms 타이머를 취소해, 180ms보다 촘촘한 출력 중 서버 resize가 계속 밀릴 수 있었다. 같은 크기의 대기/진행 요청을 공유해 최초 기한을 유지하고 연속 출력에서도 크기를 전달한다. 정상 재그리기는 오류 안내를 띄우지 않는다.

후속 피드백 — 글자가 너무 굵음: 현재 일반체는 400이나 ANSI 강조는 700이고, 터미널만 전역 antialiased를 auto로 덮어쓴다. `web/app.js`의 강조 굵기를 400으로 낮추고 `web/style.css`의 Apple 글자 가장자리 처리를 antialiased로 맞춘다. 글꼴 파일·크기·줄 간격·대비·셀 폭·실행 연결은 보존한다. 기존 frontend 검증의 굵기 기대값을 갱신해 전체 JS 검증과 공개 파일 감사를 실행하고, 같은 Mini 배포 체계에서 3개 런타임 파일만 변경한다. 체감 굵기는 실제 기기 확인과 구분한다.
