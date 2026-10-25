# 이미 완료된 Claude 세션의 종료·정리 버튼

목표: CLI가 정확한 기존 background 세션의 완료를 확인하면 종료 명령을 반복하지 않고 사용자의 정리 요청으로 연결된 터미널과 목록을 정리한다.

확인: 사용자가 제시한 Air15 Claude 행을 운영 snapshot과 실제 `claude agents --json --all`에서 같은 native id/sessionId로 찾았다. kind=background, state=done, status=idle이다. `SessionControl._observe`는 background의 ended=true를 terminate 미지원으로 반환하고, UI는 이를 ‘종료됐거나 프로세스 신원 미확인’이라는 공통 오류로 표시해 정리 버튼도 막는다. 이 사례는 터미널 ID가 사라진 증거가 아니며 실행 중인 다른 세션의 식별 실패까지 해결됐다고 일반화하지 않는다.

파일 매핑:
- 수정: `session_control.py` — 정확한 background 종료 상태와 idle/미제공 status를 확인한 별도 `claude-background-finished` capability. 실행 시 같은 target을 재조회해 이미 종료됐음을 확인하고 영수증을 남긴다. `claude stop`, attach, send 또는 프로세스 신호는 보내지 않는다. busy/waiting/미지원 상태와 신원 불명은 차단한다.
- 수정: `web/app.js` — 이미 완료된 경우 ‘끝난 세션 정리’와 정리 범위를 표시한다. 기존 원본 완료 확인 후 터미널/hold/영수증/목록 정리 경로를 재사용한다.
- 수정: `tests/test_session_control.py`, `tests/test_frontend.cjs` — done/stopped/failed의 정리 허용, native 변이 명령·kill 없음, 대상 교체·재작업 시작의 거부, 요청 재조회 중복 방지, 버튼 및 효과 설명.
- 수정: `docs/limitations.md`, `scripts/public-files.json` 및 이 계획 — 실제 지원 범위 기록.
- 참조만: `server.py`의 cleanup_terminated_session·cleanupOnly 영수증 경로, `transfer_native.py`와 remote RPC, exact 터미널 식별. 삭제·DB 변경·다른 플랫폼 권한 확장 없음.

순서/검증:
1. 실제와 같은 종료된 background fixture로 현재 버튼 미지원 상태를 재현한다.
2. 이미 종료 확인만 별도로 구현하고 관련 제어·HTTP 정리 테스트와 전체 JS 테스트를 통과시킨다. backend 종료 판정 변경이므로 전체 Python 테스트도 수행한다. 공개 파일 감사·diff 검사 후 셀프 리뷰한다.
3. 최신 운영 기록 탭 개선을 보존해 canary 후 한 번 배포한다. 실제 제보된 세션에는 preview만 요청해 정리 capability가 바뀌었는지 확인하고 실제 종료·목록 삭제는 자동 실행하지 않는다. 기존 live 터미널과 제목 저장소·파일 해시·새 오류를 검사한다.

셀프 리뷰: 원본 프로세스 신원 미확인을 완료로 간주하지 않는다. 종료 상태는 native CLI의 같은 id/sessionId/startedAt와 상태 재조회로 확인한다. 원본이 재개되거나 교체되면 새 확인을 요구한다. 이미 종료된 영수증은 기존 confirmed 계약을 사용하므로 정확히 연결된 사전 수집 터미널만 정리하고 추정·나중에 열린 터미널은 보존한다.

추가 사용자 요청 — 입력창 배경: raw `.draft-panel`만 terminal 전용 색을 사용해 공통 작성창과 달랐다. `web/style.css`에서 해당 패널 안의 색만 보드 surface/label/accent 토큰으로 통일하고 safe-area를 포함해 배경을 채운다. 터미널 출력과 키 패널의 색·입력 경로는 유지한다. 가벼운 스타일 변경은 별도 테스트를 만들지 않고 기존 전체 frontend 검증·공개 파일 검사와 최종 diff로 확인한다.
