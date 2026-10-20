# P0: Windows PC 작업의 연결 유지와 같은 세션 복귀

## 목표와 우선순위

반나절·하루 뒤 연결이 끊겨도 사용자가 새 대화를 만들지 않고 같은 기기·계정·세션 ID와 작업 폴더로 돌아오게 한다. 이 사용자 보고를 기존 메인보드 개선 계획의 **최우선 P0**로 둔다. 상태 표시·질문 영수증·관계/요약 개선은 이 뒤에 진행한다.

분석 기준은 운영 디스크 소스와 일치하는 `24c0e4d`다. 사용자 승인 뒤 별도 브랜치에서 아래 P0-1/P0-2와 P0-3의 자동 정리 보호를 구현했다. 운영 설정·실제 사용자 세션은 변경하지 않았다. 사용자가 말한 PC 작업의 도구와 끊김 당시 원인은 아직 확정하지 않았으며, 두 도구의 복귀 경로를 구분한다.

## 확인한 사실과 아직 가설인 부분

| 항목 | 근거와 판단 |
| --- | --- |
| Mac/Windows 연결 유지 구조 차이 | 원격 Mac은 `Transfers.terminal`의 remote tmux hold + hold-loop를 지원한다. Windows `windows_terminal/windows_launcher`는 직접 SSH 경로이고 hold가 없다. 2분 넘는 단절에서 실행/클라이언트가 끝날 수 있는 구조 차이는 확인됐다. 실제 제보의 단절 원인이 절전·네트워크인지는 미확인이다. |
| Codex 자동 연결 정리 | 30분 미사용 attach는 최신 목록의 phase가 idle/done/closed일 때 release한다. 운영에도 PC 정리 이력이 있다. 모든 release가 오류나 원본 세션 종료인 것은 아니다. |
| 목록 누락을 종료로 간주 | `close_idle_codex_attaches/close_idle_runs`는 Board.source 실패를 done으로 간주한다. 그러나 Windows top100 밖에도 원본 DB·대화가 남을 수 있다. 임시 DB·가짜 터미널로 재현했다. 현재 보존된 운영 연결 표본에서 목록 누락은 관측하지 않았다. |
| 읽기와 재개 대상의 resolver 차이 | `Chat.resolve`는 terminal nativeSource를 fallback으로 사용하지만 `Workflow.plan/_launch`는 Board.source를 요구한다. 정확한 원본이 남아도 목록 밖이면 읽기 성공/재개 실패가 합성 재현됐다. |
| 종료 연결 재탐색 | FE renderTerminals는 alive만 표시한다. sessionForTerminal은 현재 allSessions에서만 찾는다. 종료 화면을 떠나거나 reload하면 닫힌 연결에서 원래 작업으로 돌아가기 어렵다. |
| 보기 닫기와 목록 정리 | Handler.tidy_after_close(ended=False)도 unknown을 tidy한다. FE 기본 matches는 tidy된 unknown을 숨긴다. 보기만 닫았는데 현재 실행이 확인되지 않은 작업이 목록에서 빠지는 경로를 합성 재현했다. released 자동 정리 자체가 이 Handler를 호출하는 것은 아니다. |
| 오래된 대화의 최근 사용 | 기본 오래된 기록 필터는 updatedAt을 사용한다. 방금 openedAt이 갱신돼도 오래된 대화는 숨겨질 수 있다. 합성 재현됐다. 이것이 사용자의 개별 사례인지 미확인이다. |
| PC Codex cold resume | 기존 비상승 프로젝트 서버가 있어야 같은 ID를 연다. 서버가 꺼졌으면 새 세션으로 프로젝트를 열라는 안내를 반환한다. 현재 대표 사전 검사들은 통과했으므로 이번 제보의 직접 원인으로 확정하지 않는다. |
| PC Claude interactive | 살아 있는 대화형 프로세스는 attach를 거절하고 background+jobId만 지원한다. 프로세스가 끝났다면 저장된 같은 ID로 --resume한다. 실제 제보의 도구·프로세스 상태에 따라 원인이 다르다. |

## 구현 순서

### P0-1. 연결을 닫아도 원래 작업을 다시 찾기

사용자 변화: 종료된 보기에서 ‘원래 작업으로 돌아가기’를 사용할 수 있고 새로고침 뒤에도 정확한 작업을 찾는다.

변경 후보:

- `server.py:Handler.tidy_after_close`, terminal close route: 보기 연결 종료와 실제 작업 종료·명시 목록 정리를 구분한다. 최소 수정은 ended=False/unknown을 완료로 간주해 tidy하지 않는 것이다. 기존의 사용자가 명시적으로 지운 기록을 일괄 복원하지 않는다.
- `terminal.py:_metadata/_public/_load/_trim_closed`: 정확한 nativeSource·sourceKey·도구·계정·작업 폴더 및 연결 종료 사유를 보존한다. 표시용 종료 터미널 이력의 40개 한도와 복귀 참조의 보존 수명을 분리한다. 터미널 원문을 장기 저장할 필요는 없다.
- `server.py:Workflow`와 세션 메타 저장소: 최근 사용 세션의 작은 복귀 참조를 영구 보존한다. 기존 opened/pinned 정보와 결합하되 실행 권한이나 대상 진위를 대신하지 않는다.
- `web/app.js:renderTerminals/sessionForTerminal/showTerminalEnded/openNativeChat/matches`: 최근 끊긴 연결에서 원래 작업으로 가는 경로를 제공한다. 이전 버튼과 재개 계획 흐름을 재사용하고 새 세션 시작을 기본 복구로 제시하지 않는다. 복귀 대상이 현재 필터 밖인 경우 정확한 세션만 열고 필터·초안·스크롤을 보존한다.

검증: attach 보기만 닫은 unknown 작업의 자동 tidy 0회, 명시 종료·명시 지우기는 유지, reload 후 같은 identity의 복귀 경로 노출, 최근 열람한 오래된 기록을 복귀 동선에서 찾을 수 있음, 다른 host/home/Claude sessionId와 혼동 없음. 이 단계에서 단순 복귀 링크 클릭은 launch/send를 실행하지 않는다.

### P0-2. 목록 밖에도 같은 ID를 정확히 찾아 재개 계획 만들기

사용자 변화: 최근100개 목록에서 밀려도 기존 대화를 새로 만들 필요 없이 원래 세션을 지정해 재개 여부를 확인한다.

변경 후보:

- `windows_collector.py:database_rows/read`, `windows_runtime.py:_identity/_codex/_codex_resume/_claude_resume`: 이미 존재하는 exact ID 조회를 재사용한다. archived·cwd·home·PID/서버 소유·저장 권한을 다시 확인한다.
- `server.py:Board.source`, `Workflow.plan/_launch`, `Chat.resolve`: 서버가 저장한 nativeSource/복귀 참조에서 시작한 **조회 전용 exact resolver**와 실행 직전 재검증을 공통화한다. 브라우저가 임의로 준 경로나 명령을 실행하지 않는다. 제목·프로젝트·시각 유사성으로 대체 세션을 고르지 않는다.
- `server.py:close_idle_codex_attaches/close_idle_runs`: bounded 목록에 없다는 이유로 done 처리하지 않는다. 정확한 상태를 모르면 정리를 건너뛰거나 대상 ID 하나만 검증한다. 작업 중/질문 대기·불확실한 소유자를 종료하지 않는다.

검증: 임시 DB의 기존1개+더최근100개에서 목록은100개를 유지하면서 oldID exact 복귀 계획 성공, source 검증 실패 시 차단, 동일 ID/다른 home·host·cwd·archived·다른 writer는 거절, 재개 계획 조회에서 thread/resume·turn/start·터미널 create는0회. 실행은 명시 동작일 때만 같은 ID를 사용하고 새 대화 생성은0회여야 한다.

### P0-3. Windows 연결이 끊겨도 실행과 복귀 경로 유지

사용자 변화: 화면을 오래 보지 않거나 네트워크가 끊겨도 실행 유지·같은 세션 재연결의 지원 범위가 명확하다.

변경 후보와 선행 조건:

- `terminal.py:idle_codex_attaches/release_codex_attach`, `server.py:close_idle_codex_attaches`: Windows에서 보기 연결을 끊어도 원본 실행이 유지되고 즉시 exact 재개가 가능하다는 근거 없이 자동 정리하지 않도록 한다. 단순 타임아웃 증대만으로 해결됐다고 판단하지 않는다.
- `transfer.py:windows_terminal/windows_launcher/_windows_ssh`, `windows_runtime.py:terminal_plan/terminal_settle`: 지속 실행과 TUI 연결을 분리할 수 있는 Windows 경로를 설계한다. Mac tmux 명령을 Windows에 그대로 적용하지 않는다.
- 프로젝트 서버 재시작이 필요한 경우 실제 사용자의 비상승 실행 환경과 저장 권한을 유지하는 기존 launcher/등록 경로부터 확인한다. 상승된 SSH에서 임의 app-server·새 대화를 실행하는 우회는 사용하지 않는다. 기존 실행 서버를 확인·복원하는 일과 새 세션을 만드는 일을 분리한다.
- CLI별 제한을 안내한다. Claude의 살아 있는 interactive 실행을 검증 없는 새 --resume 프로세스로 중복 실행하지 않는다. Codex unresolved permission/다중 owner는 차단을 유지하고 안전한 복구 안내를 한다.
- `terminalEndedCopy`는 released라는 이유만으로 모든 실행이 계속된다고 단정하지 않는다. 실제 확인 근거 또는 ‘마지막 확인/현재 미확인’ 설명을 사용한다.

이 단계의 Windows 지속 실행 방식은 아직 확정하지 않았다. 정확한 제보 사례의 도구와 끊김 당시 프로세스·서버 상태를 확인하고 선택한다. 별도 실행 서비스 설치가 첫 조치인 것은 아니다.

## 검증 명령과 인수 기준

관련 테스트에 위 재현을 먼저 추가한 뒤 실행한다.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests python3 -m unittest -v test_windows_host test_windows_runtime test_windows_collector test_session_recovery test_workflow test_terminal
node --test tests/test_frontend.cjs tests/test_session_control_frontend.cjs
```

합성 테스트는 가짜 프로세스·임시 DB·SSH stub·fake clock을 사용한다. 네트워크120초 단절, 브라우저 reload, 30분/3시간 idle sweep, 목록100개 초과, 서버 재시작, 프로젝트 서버 중단, 다중 접속, stale 목록, unresolved permission을 각각 검증한다. 기존 사용자의 작업을 재실행하지 않는다.

최종 인수는 별도 전용 Windows 세션에서 반나절/하루 유지 시험과 단절 후 같은 세션 ID 복귀다. 현재 사용자 세션을 대신 새로 만드는 것으로 통과 처리하지 않는다. 연결 끊김 감소와 기록·복귀 보장을 별도로 측정한다.

## 되돌림과 남은 불확실성

구현 기록: `RecoverySessions`는 터미널 이력과 별개인 최대 500개·1MiB의 비공개 참조 저장소다. 최근 20개는 명시 정리·삭제·종료 기록을 적용해 표시한다. 목록 밖 복귀는 서버가 이미 보존한 참조만 허용하고, `recover_session`으로 원래 host/home/id/cwd/Claude sessionId를 읽기 전용 재검증한다. 조회·계획만으로 원본 실행을 시작하지 않는다. 보기 닫기는 자동 tidy하지 않으며 Windows idle/spare/run 자동 정리를 건너뛴다. 재사용한 Claude worker의 다른 대화와는 연결하지 않는다.

Windows의 새 지속 실행 서비스나 상승된 app-server를 설치하지 않았다. 실제 단절 원인과 CLI별 실행 소유가 확인되지 않아 이 확장 방식은 미확정이다. 현재 구현의 인수 범위는 **불필요한 자동 끊김 방지와 원래 ID 재탐색**이며, 네트워크 단절 후 프로세스 생존은 별도 전용 세션의 장시간 시험으로 확인해야 한다.

UI와 목록 정리 보호, exact resolver, Windows 지속 실행 변경은 별도 변경으로 나눈다. 새 복귀 저장소가 생기면 additive 형식으로 보존하고 롤백 때 원래 대화·기록을 삭제하지 않는다. 사용자 명시 종료·계정/권한 경계는 유지한다.

현재 실제 단절의 원인(절전·네트워크·보드 자동 정리·CLI 종료·프로젝트 서버 종료), 도구와 화면에서의 실패 문구, 삭제·정리 동작의 개입 여부는 확정되지 않았다. 이를 확인하지 않은 채 반나절 주기의 원인을 단정하거나 전체 사건을 단일 원인으로 합치지 않는다.
