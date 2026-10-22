# 일시적인 연결 확인을 조용하게 처리

목표: 잠깐 최신 상태를 읽지 못해도 자동 재확인을 유지하고, 입력 도중 복구 버튼이 깜빡이지 않게 한다.

확인된 경로: `chatRecovery`는 host offline 또는 stateReadSuperseded를 refresh_host로 반환하고 `updateChatComposer`는 즉시 큰 복구 카드를 표시한다. `terminalStatus`도 연결 중과 최종 실패를 구분하지 않고 connected=false면 다시 연결 버튼을 표시한다. 대화의 자동 읽기는 idle·offline host 조합에서 멈출 수 있고, SSE가 건강하면 복구 확인도 느린 안전 주기를 쓴다.

파일 매핑:
- 수정: `web/app.js` — refresh_host 카드만 15초 지연, 빠른 복구 시 타이머 취소. 열린 대화의 연결 확인은 최대 5초 간격의 기존 bounded 상태 읽기로 지속한다. resume_session은 명시적 행동이 필요하므로 즉시 유지한다. 터미널 재연결 버튼은 자동 연결 중 숨기고 읽기 재시도 소진·입력 결과 불명에만 표시한다. 브라우저 offline 안내는 online 이벤트의 자동 복구와 일치시킨다.
- 수정: `tests/test_frontend.cjs` — 짧은 장애의 무표시, 지속 장애의 표시, 정상 복구·기기/세션 이동·auth epoch 경계, 초안·IME·음성 보존, SSE 상태와 무관한 연결 재확인, 실제 전송 결과 불명과 재시도 소진의 수동 복구 유지.
- 수정: `docs/limitations.md`, `scripts/public-files.json` — 정책 기록과 이 계획 공개 목록 추가.
- 추가: 이 계획 파일. 삭제 없음. `server.py`, native 전송·세션 시작·종료·연결 판정은 참조만.

순서/검증:
1. 기존 fixture에서 즉시 버튼이 뜨는 경로와 자동 확인 간격을 재현한다.
2. UI 유예와 자동 읽기 범위를 구현하고 경계별 가짜 시계/전송 테스트를 실행한다. 버튼을 숨겨도 전송 가능 여부 판단과 결과 불명 기록은 유지한다.
3. `node --test tests/test_*.cjs`, `python3 scripts/check_public.py --release`, `git diff --check`를 통과시킨다. Mini의 최신 운영 변경을 보존한 원자적 배포로 반영하고 기존 live 터미널·파일 해시·새 traceback 없음 확인한다.

셀프 리뷰: 유예는 표시에만 적용한다. 입력/음성 DOM을 만들거나 blur하지 않고, 실패한 메시지·키를 자동 재전송하거나 종료된 native 세션을 자동 재실행하지 않는다. 실제 입력 결과가 불명확한 경우의 수동 재연결과 기존 전송 잠금은 유지한다.

추가 요청 1 — CLI 상태 해석: 운영 snapshot의 최소 상태 필드만 읽어 beaulink Claude의 status=`shell`, runtimeReasonCode=`status_unsupported`를 확인했다. `windows_collector.claude_rows`와 `windows_runtime._observe`는 busy/idle/waiting만 처리한다. 동일 레지스트리 상태를 직접 관찰한 원본 보고(https://github.com/herdrdev/herdr/issues/3414)는 shell을 foreground turn 종료·background local_bash 잔존으로 구분한다. busy/working으로 추정하지 않고 idle과 backgroundShellRunning 메타데이터로 분리한다. Mac의 명시적 state는 우선 유지하며 state 없는 shell도 처리한다. 알 수 없는 미래 상태·프로세스 신원 미확인·중복 소유자는 기존 unknown/입력 제어를 유지한다.
- 수정 추가: `collector.py`의 claude_phase, `windows_collector.py`의 공통 _claude_phase 및 행/읽기 메타데이터, `windows_runtime.py`의 표시 단계 해석. `tests/test_collector.py`, `tests/test_windows_collector.py`, `tests/test_windows_runtime.py`에서 shell·기존 busy/waiting·미래 상태·신원 불명 경계를 검증한다. 원격 사용자 입력은 보내지 않고 새 helper 배포 후 읽기 결과만 확인한다.

추가 요청 2 — 분할 화면 기기 메뉴 위치: `.top-status`는 position:static이고 메뉴는 absolute/right:8px라, 버튼 대신 전체 `.top` 헤더 오른쪽을 기준으로 열린다. 분할 버튼이 추가되면 어긋남이 커진다.
- 수정 추가: `web/style.css`에서 버튼 컨테이너를 기준 위치로 사용한다. `web/app.js`에서 열림/viewport 변경에만 메뉴 경계를 측정해 좌우 여백·가상 키보드 위 영역에 맞춘다. 버튼·입력 DOM이나 포커스는 재생성하지 않는다. `tests/test_frontend.cjs`에서 버튼 아래 정렬·좁은 창 좌우 넘침·분할 폭 변경·닫힌 메뉴 측정 생략을 검증한다.

추가 요청 3 — 첫 요청으로 자동 제목 고정:
- 확인: Windows Claude `_claude_label`은 tail의 최신 agent-name/ai-title을 사용하고, Mac Claude/Codex도 CLI name/preview를 그대로 제목으로 노출한다. 보드에서 명시적으로 수정한 제목은 `SessionLabels`가 별도 보관한다.
- 설계: native 원본 제목은 보존하고, collector가 최초 사용자 요청으로부터 `initialTitle`을 전달한다. Meta/compaction/sidechain·도구 결과·명령 wrapper는 제외하고 한 줄 60자로 정리한다. Native Claude의 명시적 custom-title은 `manualNativeTitle`로 보존한다. 표시 우선순위는 보드 수동 제목 > native 명시적 수동 제목 > 첫 요청 제목 > 기존 fallback이다. 최신 자동 제목과 마지막 답변은 initialTitle에 사용하지 않는다. 최초 요청을 읽을 수 없는 Claude는 고정 placeholder를 사용한다. Codex는 DB first_user_message 또는 thread preview의 최초 요청을 사용하며 기존 메시지 본문이나 native 설정은 수정하지 않는다.
- 수정 추가: `session_labels.py`의 기본 표시 제목 선택, `collector.py`·`windows_collector.py`의 초기 제목 메타데이터, `web/app.js`의 표시 저장소 장애 fallback. `tests/test_session_labels.py`, collector 두 플랫폼 테스트 및 frontend 검증으로 마지막 답변/ai-title 변경 뒤 고정·수동 제목 우선·세션별 분리·재시작 후 동일성을 확인한다. 기존 private alias 파일 형식은 바꾸지 않는다.
- ‘한 번 붙이면 고정’은 native preview 자체가 나중에 바뀌어도 유지되도록 `SessionLabels` 내부의 별도 private `session-initial-titles.json`에 처음 확인한 제목만 저장한다. 기존 수동 alias 파일과 형식은 유지한다. 원자적 쓰기·private 권한·정확한 host/agent/home/id/sessionId 경계를 기존 저장 계약으로 재사용하며, 새 ID가 없으면 다시 쓰지 않는다. 자동 제목은 최대 20,000개·기존 8MiB 파일 상한 안에서 보관한다. snapshot 응답 캐시 키에도 자동 제목 파일을 포함한다.
