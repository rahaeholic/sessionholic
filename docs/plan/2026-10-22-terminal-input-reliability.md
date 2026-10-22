# 연결된 터미널의 상태 표시·한글·여러 줄 입력

목표: 정확히 연결된 Claude 터미널을 저장 기록으로 오인하지 않고, 한글은 조합 가능한 입력창에서 작성하며, 여러 줄은 한 번의 bracketed paste로 전달한다.

확인과 재현:
- 제보된 PC Claude는 native collector의 runtimeKnown=false/stored_only지만 보드가 연결한 exact 터미널은 살아 있다. `Chat._claude_capability`는 해당 터미널로 전송을 허용한 뒤 runtime observation의 unknown 처리에서 다시 차단한다. 연결 여부와 native 단계 판정을 분리해야 한다.
- `terminalDocumentKey`는 편집기가 포커스를 갖지 않으면 event.key의 한글 자모를 바로 전송한다. 조합을 소유할 textarea가 없으므로 완성 글자를 기다릴 수 없다. 한글·IME 시작 키를 공통 작성창(없으면 raw 작성창)에 포커스하고 브라우저의 조합 이벤트에 맡긴다. ASCII 직접 키·명시적 Ctrl/Alt/Meta 조작은 보존한다.
- 실제 격리 tmux에서 최초 출력에는 DEC2004 enable이 있지만 fresh redraw에는 없어졌고 reset=true였다. xterm.reset이 모드를 지운 뒤 브라우저가 여러 줄을 거부한다. tmux 3.6a에는 조회 가능한 pane_private_modes가 없어 최신 tmux 기능에 의존하지 않는다. 서버가 이미 수신한 ANSI의 bracketed-paste 설정을 bounded tail로 추적하고 새 화면 메타데이터로 보낸다. 브라우저 reset 뒤 실제 관측 모드만 복구한다.

파일 매핑:
- 수정: `chat.py` — native 단계는 unknown으로 유지하면서 exact live Claude 터미널을 통한 기존 메시지 경로의 capability와 연결 메타데이터를 보존한다. 일회성·봇·추정/닫힘·다른 source·더 최근 관측이 이긴 읽기의 차단은 유지한다.
- 수정: `terminal.py` — ANSI private mode 2004의 enable/disable·RIS를 chunk 경계를 넘어 추적하고 read 응답에 bool만 추가. 화면 내용과 입력 본문은 새로 저장하지 않는다. 입력/실행 경로는 변경하지 않는다.
- 수정: `web/app.js` — fresh reset의 붙여넣기 모드 복구, 포커스 없는 한글을 작성창으로 이동, 정확한 연결과 native 단계 불명을 구분하는 상태 문구. 여러 줄은 기존 하나의 bracketed 입력과 명시적 Enter를 사용한다.
- 수정: `tests/test_chat.py`, `tests/test_terminal.py`, `tests/test_frontend.cjs` — exact/estimated·닫힘·다른 ID·known/unknown 경계, 모드 분할 수신·disable·reset·실제 PTY fresh, 한글 자모 무전송·작성창 포커스·Ctrl 단축키, 여러 줄 한 번 전달과 중복 잠금 검증.
- 수정: `docs/limitations.md`, `scripts/public-files.json` 및 이 계획. 서버 HTTP route·저장 형식·원격 설정·native CLI·임의 강제 모드는 바꾸지 않는다.

순서/검증: 위 조건을 fixture에서 재현한 뒤 수정한다. 관련 Python/JS 검증, 전체 Python·JS, 공개 파일 감사·diff 검사와 셀프 리뷰를 마친다. 앞서 배포한 기록 작성창·터미널 복구를 보존해 종료 정리·색상 수정과 한 번에 배포한다. canary 실제 PTY와 운영 해시·live 터미널 보존·제보 세션의 읽기/preview만 확인하고 실제 사용자 대화에 입력하지 않는다. 실제 Mac/iPad IME와 마이크 체감 검증은 자동 테스트와 구분한다.

참고: tmux 공식 `format.c`의 최신 pane_private_modes는 현재 운영 버전의 기능이 아니다(https://github.com/tmux/tmux/blob/master/format.c). 설치된 버전에서 확인한 실제 스트림과 격리 재현을 구현 근거로 사용한다.

추가 요청 — 집중모드 첨부: `.is-reading`이 전체 toolbar를 접으므로 기존 첨부도 사라졌다. `buildChatComposer`의 main에서 textarea 바로 앞에 같은 파일 선택기를 여는 버튼을 추가하고 집중모드에서만 표시한다. 기존 supported/offline/전송 중 잠금과 편집기 epoch 검사를 공유한다. 새 세션 raw 작성창의 기존 첨부 버튼은 유지한다.
추가 요청 — Mac 파란 줄·조합: 공통 작성창에 spellcheck/autocorrect 제어가 빠져 있고 매 input 및 상태 갱신마다 조합 중에도 높이를 0으로 바꿨다. 입력 보정은 두 작성창에서 끄고 live composition 동안 크기 계산은 건너뛰며 compositionend 뒤 한 번 갱신한다. 실제 Mac IME 재현은 별도 미검증으로 보고한다.
추가 요청 — 작업 중 후속 전송 지연: 운영의 본문 없는 SendTiming 단계 로그와 코드의 입력 잠금/CLI RPC/붙여넣기·확인 지연을 분석한다. 실행 중이라는 이유로 브라우저가 대기하거나 최신 기록 읽기와 직렬화하는지 fixture로 확인한다. 실제로 확인한 지연만 개선하고 원인 추정으로 중복 전송 보호·native 입력 확인을 제거하지 않는다.

지연 확인 결과: 본문을 출력하지 않는 운영 SendTiming 로그에서 느린 전송 32건을 확인했다. Claude의 새 attach 단계가 약 2.6~5.0초인 사례와 submit 확인 약 0.8초가 반복됐고, 기존 터미널 전송 2.13초 사례는 capabilities 0.87초·paste 0.38초·submit 0.83초였다. Codex RPC 2.22초 사례도 있었다. 현재 코드는 working에서도 기존 native 전송 경로로 즉시 요청하며 브라우저가 기록 읽기 완료나 작업 종료를 기다리는 큐를 두지 않는다. 새 테스트로 기존 Claude 터미널에는 startup settle/open이 호출되지 않음을 확인한다. 사용자 제보 시각과 각 로그의 직접 대응은 미검증이고, 중복 전송 방지와 실제 입력 확인 지연은 추측으로 제거하지 않는다.

IME 추가 경계: 전송 응답이 compositionstart와 첫 input 사이에 도착하면 revision/text가 아직 그대로여서 입력창을 비울 수 있었다. 공통·raw 작성창 모두 진행 중인 조합이 있으면 영수증 도착으로 `.value`를 지우지 않도록 보호하고 지연 응답 fixture로 확인한다.
