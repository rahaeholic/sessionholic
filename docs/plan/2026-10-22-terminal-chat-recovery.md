# 미확인 터미널의 채팅 복구

목표: 공유 서버 방식의 Codex에서 native 실행 ID를 확인하지 못해도 채팅 탭이 영구 대기하지 않고, 사용자가 고른 같은 기기의 실제 대화를 읽고 다시 열 수 있게 한다. 조회 연결을 실행 신원으로 승격하지 않는다.

확인된 원인: 운영의 미확인 Codex 두 개는 hold pane과 TUI 프로세스가 살아 있지만 rollout writer는 공유 서버에 있다. TUI의 현재 대화 ID는 argv, pane title, native 로그의 thread_id에 없다. Codex 0.163.0 소스의 TUI telemetry ID는 시작 때 별도 생성되므로 신원 근거로 사용할 수 없다. 기존 프런트는 estimated 연결을 거절한 뒤 복구 동작 없는 안내만 보여 준다.

가설: 실행 신원(nativeSource)과 사용자가 선택한 조회 대상(chatSource)을 분리하면, 추정 신원에 입력/종료 권한을 주지 않으면서 실제 대화를 읽는 기능을 복구할 수 있다. 자동으로 연결된 정상 세션의 동작은 유지한다. 자동 식별 자체가 제공된다고 주장하지 않는다.

수정 파일과 순서:
1. tests/test_terminal.py, tests/test_new_session.py: 조회 연결 저장/재시작, 저장 실패 롤백, 다른 기기/프로그램/종료 터미널 거절, exact 신원 보존과 조회 연결이 실행 제어 대상이 되지 않음을 재현한다.
2. terminal.py: chatSource를 native와 같은 엄격한 형식으로 검증하고 별도로 저장한다. link_chat은 실행 신원/confidence를 건드리지 않는다. server.py: Workflow.bind_terminal_chat 및 인증·CSRF를 따르는 POST /api/terminal/:id/conversation를 추가한다. source는 서버 목록에서 해결하고 같은 기기와 프로그램 경계를 검사한다. 입력·resume·모델 호출·종료를 실행하지 않는다.
3. web/app.js: 미확인 launcher의 채팅 탭에 대화 선택/연결 변경을 제공한다. 선택 전에는 자동 매칭하지 않는다. 선택한 기록은 기존 메시지 렌더러의 조회 전용 경로로 표시하고 최신 내용만 주기적으로 읽는다. 터미널 탭은 원래 opaque terminal ID로 돌아가며 초안을 보존한다. 다른 창/탭/인증으로 바뀐 뒤 도착한 응답은 무시한다. nativeSource exact 판정과 공통 작성창/종료 로직은 완화하지 않는다. web/style.css: 작은 분할 창/모바일에서도 선택 UI와 기록이 정상 배치되게 한다.
4. tests/test_frontend.cjs: 선택 연결, 새로 열기, 기록 렌더링, 바뀐 source/터미널/인증의 늦은 응답, 조회 실패 뒤 기존 기록 보존, 실제 입력/launch/close 요청이 없음을 검증한다.
5. README, docs/limitations, scripts/public-files.json 갱신. 관련 Python, 전체 JS, 공개 감사와 diff check 후 격리 canary와 운영 배포 한 번. 실제 사용 세션에 테스트 입력/종료를 보내지 않는다.

검증 명령: python3 -m unittest tests.test_terminal tests.test_new_session, node --test tests/test_*.cjs, python3 scripts/check_public.py. 운영에서는 제공 파일 해시, 기존 터미널 ID 보존, 조회 연결 API 제공과 실제 native 기록 조회를 확인한다. 사용자가 대화를 선택하지 않은 운영 터미널에 임의 연결을 저장하지 않는다.

참조만: launcher_programs.py, chat.py, transfer_worker.py, native_chat.py, Codex upstream rust-v0.163.0. 전역 CLI 설정, 사용자 런처, 업무 프로젝트, 공유 daemon은 변경하지 않는다.

추가 검증/조정: chat.py:resolve/source에서 서버가 저장한 chatSource는 읽기 요청에만 목록 외 복귀를 허용하고, 보내기 경로에는 사용하지 않는다. tests/test_chat.py로 목록 탈락 뒤 읽기 성공·보내기 신원 거절·계정 경계 유지를 확인한다.

추가 사용자 지시: 일반 세션의 기본은 채팅으로 바꾼다(web/app.js:sessionDetailMode). web/workspace.js의 준비 중인 창 선택과 드래그 드롭도 채팅으로 열며, 명시적인 터미널 열기/기존 보기 복원은 유지한다. tests/test_frontend.cjs 및 tests/test_workspace.cjs로 기본 채팅과 명시 터미널을 각각 검증한다. 새 세션의 런처 선택 단계는 기존 터미널에서 진행한다.
