# 종료 관리 재검토

목표: 한 번 접수한 종료 요청의 실행 여부와 남은 정리를 다시 확인할 수 있고, 미확인 요청 때문에 사용자가 결과 확인 창에 갇히지 않게 한다.

초기 리뷰 기준 코드는 `06b7888`이며 아래 구현 현황을 함께 관리한다. 배포 여부는 마지막 검증 결과를 따른다. 기존 `2026-10-22-session-termination.md`의 “재시도는 영수증 조회만, observe 미호출” 조건은 **이미 성공한 요청에만** 적용한다. 미확인 요청은 명령 재전송 없이 실제 상태를 다시 읽어야 한다.

## 확인된 사실과 앞선 설명 정정

| 구분 | 근거 | 결론 |
| --- | --- | --- |
| 재현한 결함 | `web/app.js:terminateAndCleanup`이 HTTP 전송 전에 요청을 저장하고, 예외에서는 지우지 않는다. CSRF 403 뒤 재클릭하면 `cleanupOnly`만 전송한다. | 명령이 전달되지 않았어도 존재하지 않는 결과를 계속 조회할 수 있다. |
| 재현한 결함 | `windows_runtime.py:execute`의 `receiptOnly`가 미확인 결과도 그대로 반환한다. 합성 원본을 archived·unloaded로 바꿔도 재조회 native 호출 0회, 결과는 계속 unconfirmed다. | Windows에 미확인 요청의 종료 후 상태 재검증이 없다. |
| 추가 설계 제약 | Windows `_observe`는 이미 보관된 대화를 `target=None, phase=unknown`으로 반환한다. | 단순히 `_observe` 호출을 추가하는 수정으로는 해결되지 않는다. 저장된 종료 대상 전용 읽기가 필요하다. |
| 코드와 운영 메타데이터 확인 | Mac `_reconcile`은 최신 marker의 requestId가 다르면 옛 결과를 반환한다. 실제 옛 미확인 기록과 같은 source의 더 새로운 성공 marker가 함께 존재한다. | 이전 요청이 대체됐다는 상태를 돌려줘야 한다. 다른 요청의 성공을 무조건 복사하면 새 실행까지 정리할 수 있다. |
| 앞선 해석 정정 | `managed_launch._resume`은 Codex `--remote` TUI를 연결하고, `_codex_resume_argument`는 이를 PID 종료 대상에서 제외한다. | 제외 자체는 버그가 아니다. 화면 프로세스 종료와 원본 thread 종료는 다르다. 매처를 넓히거나 공용 daemon을 죽이는 해결책은 제외한다. |
| 완료 범위 보완 | 현재 Codex 종료는 선택 thread의 상태를 확인한다. Windows는 `descendantsConfirmed=False`도 반환한다. | 부모 성공만으로 하위 에이전트·별도 도구 프로세스까지 모두 끝났다고 표시하지 않는다. |

공식 [Codex App Server 문서](https://learn.chatgpt.com/docs/app-server)는 archive가 자손 보관도 시도하지만 일부 자손이 실패해도 부모 요청은 성공할 수 있다고 설명한다. loaded 목록은 메모리에 올라온 thread 목록이며 OS 프로세스 종료 증명은 아니다. unsubscribe는 연결 구독 해제이므로 종료의 대체 수단으로 쓰지 않는다. 최신 문서의 실험적 하위 thread·background terminal API는 설치된 CLI 버전에서 지원 여부를 먼저 확인해야 한다.

미검증: 최근 제보의 정확한 클릭과 요청 ID를 특정하지 못했다. 조회한 Windows 운영 저장소에는 종료 영수증이 없어, Windows 결함을 이번 제보의 실제 원인이라고 단정할 수 없다. SSH 응답 제한과 native 조회 시간이 겹쳐 응답이 끊기는 가능성은 코드상 존재하지만 실제 원인 비율은 측정하지 않았다.

## 구현 순서

1. **미전달과 결과 유실을 구분한다.** `server.py:route_post/session_control`, `transfer.py`, `transfer_worker.py`, `web/app.js:terminateAndCleanup/confirmEndSession`을 수정한다. CSRF·대상 검증 등 native dispatch 이전 거절에는 구조화한 `not_started` 근거를 준다. 이 경우에만 보류 기록을 해제하고 인증·대상 미리보기를 갱신한다. HTTP 오류 번호만 보고 재전송하거나, 응답 유실·영수증 부재를 미전달로 간주하지 않는다.
2. **미확인 요청을 읽기만으로 복구한다.** `windows_runtime.py:execute`에 종료 직전 method·계정·cwd·thread/endpoint/process 시작 신원을 저장하고 전용 reconciliation을 추가한다. 새 실행용 `_observe`와 분리해 archived·unloaded를 읽는다. `session_control.py:_reconcile/receipt/close_provenance`는 옛 marker·불완전한 구형 영수증에 `superseded`, `insufficient_evidence`, `host_unreachable` 등 이유를 반환한다. 새로운 실행이 있으면 옛 요청으로 정리하지 않는다.
3. **브라우저 밖에서 요청을 추적한다.** 작은 `termination_jobs.py` 저장소와 `server.py` 연동으로 요청 ID·대상 실행 신원·전달 단계·정리 단계를 원자적으로 저장한다. 같은 실행에 대한 여러 탭/기기의 종료 요청은 기존 작업 ID로 합친다. 보드 재시작 후에도 조회를 재개한다. native 변경 직전에는 호스트의 기존 receipt를 먼저 보존한다. 전송과 결과 저장 사이의 장애는 exactly-once를 보장한다고 주장하지 않고 미확인으로 남겨 읽기로 복구한다.
4. **종료와 정리의 범위를 명확히 한다.** `server.py:cleanup_terminated_session`, `session_control.py`, `windows_runtime.py`, `web/app.js:terminationCopy`를 수정한다. 원본 종료, 하위 실행 확인, 보관, 연결 터미널 닫기, 목록 정리를 별도 단계로 관리한다. 확실한 원본 종료 뒤에는 종료 명령을 반복하지 않고 남은 정리만 재시도한다. 요청 전에 잡은 터미널 ID와 실행 신원을 함께 확인하며, 새로 재개된 세션을 숨기지 않는다. 불확실한 연결은 남은 개수와 이유를 표시한다.

새 호스트 상주 서비스는 이번 선행 조건에서 제외한다. 우선 기존 helper와 보드 안에서 제한된 읽기 재확인을 구현하고, 단계별 소요 시간·실패 코드·CLI 버전·시도 횟수를 본문/비밀값 없이 기록한다. 기기 재부팅·보드 단절에도 독립적인 종료 작업 지속이 반드시 필요한 것으로 확인되면 그때 소유권·만료·중복 작업 제어를 갖춘 상주 worker를 검토한다.

실행 신원은 대화 ID 하나로 대신하지 않는다. host·account·native ID에 해당 실행의 증거를 결합한다. CLI는 PID+시작 시각+실행 파일, 공유 Codex 서버는 endpoint 세대+thread/turn 상태가 필요하다. 새로 생성하는 전용 프로세스 그룹만 그룹 종료 후보가 될 수 있고, 공유 서버의 프로세스 그룹이나 cwd 일치만으로 종료 대상을 넓히지 않는다. 채팅 기록 연결(`chatSource`)은 제어 권한의 증거가 아니다.

## 검증과 완료 조건

- `tests/test_session_control_frontend.cjs`, `tests/test_session_controls_http.py`: CSRF/인증 만료/검증 거절→새 미리보기, 응답 유실→동일 요청 조회, 새로고침·앱 재시작·다른 기기·동시 클릭→중복 native 명령 0회. 실제로 전송하지 않은 요청과 실행 후 응답만 잃은 요청을 별도 테스트한다.
- `tests/test_session_control.py`, `tests/test_windows_runtime.py`: 보관 뒤 늦은 unload, DB 반영 지연, 타임아웃, endpoint 재시작, PID 재사용, 원본 재개, 일부 자손 잔존, marker 교체, 구형 기록, 전체 목록 조회 실패를 주입한다. 현재 작업이나 이웃 세션에 신호를 보내지 않음을 검사한다.
- `tests/test_termination_jobs.py` 신규: 저장 직전/직후·전송 직전/직후·원본 성공 후 정리 전 장애, 중복 요청, 잠금 충돌, 재시작 후 읽기 복구. 성공 영수증 재조회는 원본을 재실행하지 않는다.
- 부분 정리는 단계별 실패를 남기되 종료 성공을 다시 미확인으로 되돌리지 않는다. 원본이 다시 실행되면 옛 요청으로 새 터미널/목록을 정리하지 않는다. 오프라인과 “프로세스 없음”을 혼동하지 않는다.
- 구현 검증 명령: `python3 -m unittest discover -s tests -p 'test_session_control*.py' -q`, `python3 -m unittest discover -s tests -p test_windows_runtime.py -q`, 신규 job 테스트, `node --test tests/test_session_control_frontend.cjs`. 최종 관련 회귀·공개 파일 감사 후 한 번 배포한다.
- 실제 종료 검증은 명시적으로 만든 폐기 가능한 CLI 세션에서만 한다. 기존 사용자 작업은 읽기만 한다. Mac/Windows의 실행 방식별 지원 범위와 미검증 항목을 구분해 보고한다.

이번 리뷰에서 기존 관련 Python 141개와 JS 11개는 모두 통과했다. 별도 합성 진단으로 프런트의 미전달 고착과 Windows의 결과 재조회 고착을 재현했다. 기존 테스트 통과가 해당 두 경로의 정상 동작을 입증하지는 않는다.

## 구현 현황 (2026-10-23)

- `termination_jobs.py`: 표준 SQLite 저장소로 접수·요청 합치기·재시작 복구·2초~5분 간격의 제한된 재확인을 구현했다. 증명 토큰과 대화 본문은 저장하지 않는다. 같은 source의 미완료 요청은 하나로 합치며, 기존 요청 ID는 완료 후에도 다시 실행하지 않는다. 최대 10,000개를 보존하고 한도 초과 시 새 실행 전에 거절한다.
- native receipt가 없으면 같은 native 잠금 안에서 취소 표식을 먼저 저장한다. 늦게 도착한 최초 실행도 이 표식을 확인하므로 “없음” 조회와 실제 전송의 경합 때문에 이중 종료되지 않는다.
- Windows는 종료 직전 endpoint/PID 시작 신원과 대상 정보를 저장하고, archived DB와 전체 loaded 목록을 다시 읽는다. Mac은 marker 교체·새 실행·구형 근거 부족을 구분한다. `--remote` PID 제외와 vendor remote 지원 제한은 유지했다.
- 서버는 최초 종료 전에 터미널 ID와 연결 신원을 보관한다. 새 연결이나 새 실행을 발견하면 옛 요청의 정리를 중단하고 목록을 남긴다. 실패한 종료 결과 확인과 터미널 정리는 요청 창을 닫아도 계속하며 새 앱·다른 기기는 미리보기에서 같은 요청을 찾는다.
- 인증·CSRF·대상 검증의 전송 전 거절은 `not_started`로 분리했다. 화면은 보류를 해제하고 새 미리보기를 읽으며 원본 종료는 자동 재전송하지 않는다. 종료 성공과 하위 실행 미확인 범위를 구분한다.
- 별도 기기 상주 서비스·공용 서버 강제 종료·프로세스 그룹 종료는 추가하지 않았다. 검수용 서버에서는 `--no-termination-worker`로 복사한 운영 요청의 재확인을 막는다.

검증: 전체 Python 회귀 1,595개와 프런트엔드 778개 통과. 이후 마지막 turn 비교·증명 토큰 비저장 보완은 해당 Python 테스트 169개(제어 47·Windows 93·HTTP 22·job 7) 및 프런트 제어 15개로 확인한다. 공개 파일 감사 191개, 지적 없음. 실제 사용자 세션에 종료·입력은 보내지 않았고, 직접 신호 검사는 테스트가 생성한 폐기용 프로세스에만 수행했다.

실제 Windows 읽기 검사는 archived 대상 0개여서 종료 후 readback 표본을 확인하지 못했다. 이 경로는 합성 DB·endpoint fixture로 검증했으며 운영 사용자 세션을 종료해서 표본을 만들지는 않았다.
