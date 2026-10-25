# 루나 감사 후속 개선

목표: 확인된 입력·동시 작업·목록·설정 복구·업데이트 결함 6개와 활성 분할창 닫기 단축키를 순서대로 고치고, 검증된 변경을 한 번에 배포한다.

기준 HEAD: 7390482. 원인과 재현은 완료된 감사 결과를 사용한다. 미검증 제목 가설·공유 daemon 연결 제약·실기기 IME 빈도는 이번 수정의 확정 범위에 넣지 않는다.

## 순서와 변경 파일

1. `web/app.js`의 `select`, composition commit 경로: 조합 중 세션 변경은 원 편집기 확정 후 진행한다. `tests/test_frontend.cjs`에서 늦은 final input 보존·빠른 연속 선택·인증 변경·새 초안 보호를 검사한다.
2. `windows_runtime.py:_command`: 모델 목록 읽기를 변경 작업 잠금에서 분리한다. `tests/test_windows_runtime.py`에서 목록 조회 대기 중 다른 세션 입력 및 원본 변경 거절을 검사한다.
3. `termination_jobs.py:submit/run/tick`: 동일 source만 직렬화하고 다른 source의 RPC 대기를 분리한다. `tests/test_termination_jobs.py`에서 교차 source 진행·동일 source 최초 실행 1회·재시작 readback·정리 중복 방지를 검사한다.
4. `bot_requests.py:_hermes`: 최신 활동 기준으로 LIMIT 후보를 고른다. 원본 DB 인덱스/스키마는 변경하지 않는다. `tests/test_bot_requests.py`에서 100건 경계·구형 열·정렬·큰 합성 DB를 검사한다.
5. `windows_runtime.py:_command/_model_preferences`, `native_chat.py:_codex_command/_preferences`: 모델 설정과 영수증 사이 장애 후 현재 저장된 설정을 읽어 요청값과 비교한다. 일치하면 현재 상태를 확인하고, 불일치하면 명시적 재선택을 안내한다. 과거 명령 성공을 추정하거나 설정을 덮어쓰지 않는다. 관련 Python 테스트에서 저장 전후 실패·후속 설정·구형 기록·원본 변경을 검사한다. `/compact`는 자동 재실행하지 않는다.
6. `cli_maintenance.py:update_npm`: 같은 core의 prerelease는 stable보다 낮게 비교한다. `tests/test_cli_maintenance.py`에서 beta→stable 및 동일/높은 버전 유지 검증.
7. `web/workspace.js:shortcut/handleKey/closePane`, `web/index.html` 단축키 도움말: Control+Command+W로 현재 창만 닫는다. 세션 실행과 기존 초안/음성/전송 가드 유지, 마지막 창 보호, 루트/iframe 동일 처리. `tests/test_workspace.cjs`, `tests/test_frontend.cjs` 검증. README의 키 안내도 갱신한다.

새 런타임 모듈·의존성·삭제 파일은 예정하지 않는다. `server.py`, session controls, transfer worker는 호출자 계약을 참조한다. 필요한 범위 밖 리팩터링을 하지 않는다.

사용자 추가 요청: 목록·대화 세션 메뉴의 ‘새 창에서 보기’. 기존 session 링크와 사용자 클릭에서 동기적으로 여는 팝업을 사용한다. `window=1` 창은 저장된 분할 배치의 복원/저장을 하지 않고 단일 채팅으로 시작한다. 원 창의 초안·배치 보존, opener 분리, 차단 시 안내, 별도 창의 새로고침·질문 대기 세션 동작을 검증한다.

## 완료 검증

각 단계의 관련 테스트 → 전체 `python3 -m unittest discover -s tests -q`, `node --test tests/*.cjs`, `python3 scripts/check_public.py`, `git diff --check` → diff 셀프 리뷰 → 단일 release의 격리 검수 → 운영 배포 → 관리 파일/실제 제공 자산 hash·기존 터미널 보존·새 오류 확인.

실제 사용자 세션에 테스트 입력/종료를 보내지 않는다. 배포 자동 재확인 worker가 복사된 운영 요청을 처리하지 않도록 격리 fixture 검수만 사용한다. 실기기 IME/키보드 검증과 합성 검증은 구분해 보고한다.
