# 메인보드 개선 구현 계획 — 2026-10-21

> 사용자 추가 제보 반영: Windows PC 연결이 반나절·하루 뒤 끊기고 원래 작업으로 복귀하지 못하는 문제를 **P0**로 올린다. 먼저 [Windows 세션 유지·복귀 계획](windows-session-recovery-20261021.md)을 진행한다. 아래 M1a/M2가 첫 개발 단위라는 기존 서술은 이 P0 이후의 순서로 해석한다. 아직 원인이 하나로 확정되거나 운영 수정된 것은 아니다.

## 목표와 기준

사용자가 같은 작업의 실행 상태·질문 응답 결과·하위 대화를 기기와 계정 경계를 지키면서 정확하게 확인하고 돌아갈 수 있게 한다.

분석 기준은 `24c0e4d0b0e27f18a62a5933d57d1cfbf04c8af3`이다. 이 계획을 사용자 승인 뒤 별도 브랜치/작업 트리에서 구현했다. 기존 체크아웃과 운영 설정은 보존했다. 아래 내용은 구현 기준이며 실제 적용 범위와 미검증 인수 항목을 함께 기록한다.

확인된 공백:

1. `web/app.js:allSessions/noteLivePhase`는 목록의 `runtimeKnown=false`를 상세의 확실한 상태로 승격하지 못하고, 반대로 상세의 `runtimeKnown=false`가 목록의 확실한 실행 상태를 지울 수 있다.
2. `detailRevision`에 `runtimeReason`이 없어 이유만 달라지면 열린 헤더가 갱신되지 않는다. `Chat._read_state`도 이 이유를 전달하지 않는다.
3. `WindowsCollector.read`의 Codex 저장기록 fallback은 목록에서 사용한 정확한 exec writer 근거를 재확인하지 않는다. Claude의 미확인 분기는 원인 문구를 생성하지 않는다.
4. 질문 선택 응답은 `Chat.prompt_results` 메모리에만 남는다. 같은 요청을 미확인 응답 뒤 새 Chat 인스턴스에 보내면 키가 다시 전달되는 합성 사례를 재현했다. 일반 메시지·터미널 입력의 영구 영수증과 구분해야 한다.

확인된 기존 기능은 유지한다: 네 가지 상태 그룹, 검색·고정·읽음, 전체 세션 식별, 목록 재사용과 스크롤, 초안·IME, 다섯 이어하기 모드, native 메시지 영수증, 터미널 입력 원장, 봇 conversation/native 참조, 보존 대화 요약. 목표 화면을 통째로 복원하거나 새 실행 서버를 추가하지 않는다.

## 구현 순서와 변경 지도

사용자 추가 요청(구현 중): 상세 대화창 안에 **채팅 / 터미널** 모드를 둔다.
채팅에서 터미널을 열면 같은 영역에서 전환하고, 세션·초안·스크롤을 유지한다.
M2의 영수증 보호 뒤에 반영하며 별도 터미널 목록의 기존 관리 기능은 보존한다.

| 순서 | 사용자에게 생기는 변화 | 파일·함수 | 의존 관계 |
| --- | --- | --- | --- |
| M1a, 첫 개발 단위 | 목록과 상세가 기존의 확실한 상태 근거를 일관되게 표시하고 미확인 이유가 갱신됨 | `windows_collector.py:_row/_held/claude_rows/read`, `windows_runtime.py:Runtime.chat/_state`, `chat.py:Chat._read_state`, `web/app.js:allSessions/noteLivePhase/detailRevision/detailMetaChildren/refreshDetail`, 관련 기존 테스트 | 독립 착수 가능 |
| M2, 두 번째 개발 단위 | 질문 응답의 결과가 불명확하거나 서버가 재시작해도 같은 선택 요청을 재전달하지 않음 | 새 `prompt_receipts.py`, `chat.py:Chat.__init__/prompt/_answer_prompt`, `web/app.js:promptAction/renderPrompt/chatEntry`, `tests/test_chat.py`, 새 `tests/test_prompt_receipts.py`, FE 테스트 | M1a와 별도 변경으로 검토 가능 |
| M1b | 현재 확인과 마지막 확인을 구분하고 오래된 실행 상태를 현재 상태로 고정하지 않음 | `server.py:Board/HostStream/SnapshotResponses`, `chat.py:state/_read_state/_stale`, `events.py:state_digest/_read/_watch/_next_delay`, Windows 수집기와 FE | M1a 뒤 별도 설계·검증 |
| M3a | 부모와 하위 대화를 같은 상세에서 이동 | 새 `web/session_relations.js`, `web/index.html`, `web/app.js`, `web/style.css`, `web/sw.js`, 새 관계 테스트 | M1a 뒤 가능, 신뢰성 변경과 분리 |
| M3b | 선택한 대화의 마지막 요청·저장 답변·명시 다음 행동과 원문을 짧게 확인 | 새 `web/session_context.js`, 필요 시 새 `session_context.py`, `session_history.py`, `automatic_work.py`, FE, 필요할 때만 Handler의 선택 읽기 API | M3a 뒤; 자동 작업 기능을 켜지 않음 |
| M4 | CLI/수집기 업데이트의 지원 범위를 명확히 확인 | `server.py:HostStream.run`, `collector.py:WATCH_PROTO/claude_phase/codex_phase`, capability·회귀 테스트 | 앞선 변경과 별도 단위 |

새 파일을 실제 릴리스에 포함할 때 `scripts/public-files.json`에 해당 파일·테스트·계획 경로를 등록하고 `scripts/check_public.py --release`를 통과시킨다. M2의 원장은 서버 로컬 모듈이므로 remote worker bundle에 추가할 필요가 없다. M3의 새 정적 파일 이름은 `server.py:STATIC/StaticAssets.manifest`의 자동 발견 조건에 맞추고, index의 자산 해시에 포함되는지 확인한다. `web/sw.js:ASSETS`에는 새 script를 추가한다. 서버에 수동 파일 목록을 새로 만들 필요는 없다.

삭제할 파일은 없다. `goals.json`, `automatic-work.json`, 원본 대화·계정 설정·인증은 마이그레이션하지 않는다.

## M1a — 기존 근거의 상태 합성과 원인 전달

### 작업

1. 기존 테스트에 먼저 실패 재현을 추가한다.
   - 같은 route의 목록 unknown/known=false + 상세 working/known=true가 승격되지 않는 사례.
   - 목록 working/known=true + 저장기록 상세 unknown/known=false가 목록을 내리는 사례.
   - 이유 문구만 추가/변경/삭제한 경우 열린 상세 갱신 사례.
   - exec writer lock으로 working인 정확한 ID를 저장 대화로 읽는 사례.
2. `Chat._read_state`가 `runtimeKnown`을 readOnly 여부와 무관하게 명시적으로 전달한다. 기존 결과에 해당 필드가 없으면 새로 true를 추정하지 않는다. `runtimeReason`을 기존 redaction/길이 제한을 거쳐 전달한다. `_stale`은 이전 known을 현재의 확인으로 승격하지 않는다.
3. `WindowsCollector.read`의 저장 fallback에서 해당 ID 하나의 `writer_owners({sid})`만 기존 제한 안에서 다시 확인한다. `_held`의 정확한 PID·시작 세대·실행 파일·source=exec 조건을 공유한다. 기존의 전체 lock 64개·확인 기한을 늘리지 않는다. lock 소유가 없어졌다고 완료를 만들어내지 않는다.
4. Claude는 실제 분기별 이유를 만든다. 기록만 있음, 프로세스 신원 미확인, registry 중복, 지원하지 않는 상태를 구분한다. 63개 등 특정 관측 수에 맞춰 일괄 문구를 채우지 않는다.
5. FE의 overlay는 phase뿐 아니라 known/reason을 한 묶음으로 보존한다. 다음 규칙을 두 함수에 함께 적용한다.
   - 정확한 `host+agent+home+id`, 현재 선택·읽기 세대, `stale !== true`를 확인한다.
   - 목록 known=false는 **상세가 명시적으로 runtimeKnown=true**인 경우에만 넘어설 수 있다.
   - 상세 known=false와 단순 저장 답변은 유효한 목록 known을 덮지 않는다. 상세의 ‘이번에는 실행을 확인하지 못함’ 안내는 별도로 유지한다.
   - 목록도 known=false이고 새 상세도 known=false이면 unknown을 유지한다.
   - metadata가 없는 기존 Mac 응답의 기존 경로를 유지하되, 이를 Windows unknown 승격 근거로 사용하지 않는다.
   - runtime/read 지원이 입력·중단·종료 권한을 부여하지 않는다. 실행 capability는 기존 native owner 검증 결과만 사용한다.
6. `detailRevision`에는 reason을 포함하되, 헤더 메타 갱신과 대화 본문 재조회를 분리한다. 설명 변경만으로 transcript를 새로 읽지 않는다.
7. 같은 phase의 오래된 overlay가 영구히 남는 문제의 최종 해결은 M1b에 둔다. M1a만 배포하고 확인시각·TTL·실시간 정확성이 모두 해결됐다고 보고하지 않는다.

### 데이터/API와 호환

새 endpoint는 없다. `/api/chat/state`의 선택적 `runtimeKnown`, `runtimeReason` 전달을 보완한다. 기존 phase·route·capability·receipt 구조와 4중 식별을 유지한다. native status를 읽을 수 없으면 unknown/원인으로 남기고 app-server를 자동 시작하지 않는다.

### 검증과 완료

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests python3 -m unittest -v test_windows_collector test_windows_runtime test_chat test_server test_events
node --test tests/test_frontend.cjs tests/test_session_control_frontend.cjs
```

기대 결과는 관련 테스트 전부 통과, 위 네 실패 재현의 수정 확인이다. 다른 host/home/ID, stale read, PID 재사용, 중복 endpoint, legacy unknown 보호, 선택 전환 중 늦은 응답을 포함한다. unknown이 임의 idle/closed로 바뀌거나 입력 capability가 넓어지면 미완료다.

별도 합성 Windows 세션에서 목록→상세→목록을 열어 배지·그룹·이유가 일치하는지 확인한다. 읽기 검증에서 `turn/start`, `thread/resume`, `send_keys`, `paste`, terminal create는 0회여야 한다. 실제 모바일·IME·스크롤 확인은 개인 Chrome 연결이 가능한 환경에서 따로 수행한다.

되돌림은 FE 합성 변경 및 선택적 응답 필드를 되돌리는 범위다. 원본 세션·기록·프로세스를 변경하지 않는다.

## M2 — 질문 선택의 영구 영수증

### 작업

1. 새 `prompt_receipts.py:PromptReceipts`를 만든다. `lookup`, `reserve`, `finish`, `cancel_not_sent`를 제공하고 서버 state-dir의 `prompt-receipts/receipts.sqlite3`에 원장을 둔다. 초기 한도는 10,000행·16MiB로 상수화하고, 임시 DB로 한도 도달 전 차단을 검증한다. `terminal_input.py`의 private 경로·SQLite FULL commit·내용 해시 정책을 참고하되 터미널 bytes accepted와 질문 answered를 같은 상태로 합치지 않는다.
2. 키는 서버가 resolve한 conversation identity와 requestId다. Claude는 재사용 가능한 worker id에 더해 sessionId·cwd를 묶는다. 요청 signature에는 fingerprint·number·정확한 terminal identity를 넣는다. 질문 본문·원문·터미널 입력·인증은 저장하지 않는다.
3. `_answer_prompt`는 화면과 대상 검증 뒤, **send_keys 전에** durable unconfirmed 예약을 commit한다. 기존 레코드가 있으면 보내지 않고 결과를 반환한다. 다른 signature로 같은 ID를 사용하면 409로 거절한다. 기존 conversation lock을 유지하고 RPC 동안 DB transaction을 잡지 않는다.
4. `send_keys` 직전 또는 직후 프로세스가 죽으면 예약은 unconfirmed로 남는다. `InputNotSent`처럼 키가 전달되지 않았음을 확실하게 아는 예외만 예약을 취소한다. 나머지는 자동 재전송하지 않는다.
5. answered/unconfirmed 최종 상태를 저장한 뒤 응답한다. 저장 실패는 전송 전 실패로 바꾸지 않는다. 저장된 질문 화면을 재생하지 말고 현재 화면 조회와 receipt를 분리한다.
6. `Chat.prompt`에 읽기 동작 `action: "status"`를 추가한다. 이 동작은 원장을 읽고 상태만 반환하며 터미널 attach·새 입력·재개를 하지 않는다. 기존 open/answer 요청은 유지한다.
7. FE는 요청 시작 때 생성한 requestId를 선택·인증·conversation identity에 묶는다. 통신 단절 후 ‘전달 상태 확인’은 status만 호출한다. 명시적 새 응답과 기존 요청 확인을 구분한다. 초안 저장을 사용할 때는 기존 버전 호환 형식에 작은 pending descriptor만 보관하고 질문 본문을 추가로 저장하지 않는다.
8. 상태를 알 수 없거나 원장 한도/손상/디스크 부족이면 새 키를 보내지 않고 터미널 확인 안내를 낸다. 기한 경과만으로 unconfirmed 기록을 삭제해 재전달을 허용하지 않는다. 영구 종료가 입증된 대상만 정리한다.

### 데이터/API

기존 `/api/chat/prompt`에 status 동작과 선택적 receipt 정보만 추가한다. status 응답은 `{requestId, status: "answered"|"unconfirmed"|"not_found", number, reasonCode}`다. `not_found`는 다른 기기/구버전에서 처리되지 않았다는 증명이 아니며 자동 재전송 근거로 사용하지 않는다.

이 변경은 at-most-once 보호다. unanswered를 반드시 처리하거나 모델이 답을 반영했다고 보장하는 기능이 아니다.

### 검증과 완료

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:tests python3 -m unittest -v test_prompt_receipts test_chat test_terminal_input
node --test tests/test_frontend.cjs
```

필수 검증:

- 현재 합성 재현의 ‘같은 요청, 새 Chat 인스턴스, 같은 state-dir’에서 총 send_keys가 2회에서 **1회**가 된다.
- 별도 프로세스를 예약 직후·키 전송 직후·최종 저장 직전에 종료한 fault injection에서도 동일 요청 재실행은 0회다. 실제 CLI 대신 가짜 writer와 임시 DB를 사용한다.
- 동시 탭, 같은 ID의 다른 선택지, 같은 worker의 바뀐 sessionId, fingerprint가 같은 새 질문, 원장 손상·한도·저장 실패, 인증·선택 전환을 확인한다.
- explicit InputNotSent만 재시도 가능하며 transport unknown은 status로만 확인한다. status/open은 키를 보내지 않는다.

FE 변경만 되돌려도 backend guard는 유지할 수 있다. unresolved 원장이 있는 동안 옛 메모리 전용 backend로 바로 되돌리면 보호가 사라지므로, backend rollback은 answer를 차단하는 버전으로 하거나 영구 guard를 유지한다. 원장을 지우는 것을 복구 방법으로 쓰지 않는다.

## M1b — 관측 근거·순서·만료 계약

M1a와 별도로 구현한다. 1차 적용 대상은 Windows one-shot poll과 선택한 Chat read다. Mac resident heartbeat는 다음 단계로 남긴다.

선택적 schema 초안:

```json
{
  "runtimeObservation": {
    "version": 1,
    "known": true,
    "phase": "working",
    "evidence": "exec_writer_lock",
    "reasonCode": "exec_writer_lock",
    "requestSeq": 12,
    "acceptedSeq": 15,
    "bootId": "server-generation",
    "observedAt": 0,
    "expiresAt": 0
  },
  "lastKnown": null
}
```

- evidence는 project_app_server/claude_registry_process/exec_writer_lock/external_cli_open/saved_record/unsupported 등을 구분한다. process가 있다는 사실과 모델이 응답 중이라는 사실을 구분해 표시한다.
- 원격 epoch와 로그 updatedAt을 비교하지 않는다. 같은 서버의 공통 관측 발급기에서 RPC 시작 전에 requestSeq, 수락 시 acceptedSeq·observedAt을 부여한다. 늦게 도착한 오래된 요청이 최신 결과를 덮지 않는다. 캐시 반환·stale 응답은 관측을 새로 만들지 않는다.
- 새로운 unknown은 이번 관측 실패 사유로 보존한다. 유효 known을 즉시 지우지 않되 만료되면 현재 상태는 unknown, 마지막 확인은 lastKnown으로 표시한다. 종료를 추정하지 않는다.
- 초기 lease 제안은 `max(120초, 2*실제 Windows 호스트 수집 주기+15초)`와 선택 read 90초다. 120초는 전체 기기의 고정값이 아니다. 실제 주기·최대 조회 기한과 fake clock 테스트를 기준으로 상수를 확정한다.
- 관측시각만의 변화는 `events.state_digest`에서 제외한다. selected watcher의 **실제 fresh 성공**은 기존 SSE에 최대 30초마다 작은 observation 갱신을 보낸다. 같은 digest면 FE는 lease만 갱신하고 추가 GET을 만들지 않는다. 실패·cached read는 연장하지 않는다.
- Windows poll은 기존 version 경로로 snapshot 캐시를 무효화한다. Mac의 시각 메타만 별도로 갱신할 때는 `Board.observationRevision`을 `SnapshotResponses.key_of`에 넣어야 한다. semantic version/SSE와 body cache revision을 분리하지 않은 채 timestamp만 제외하면 안 된다.
- watcher heartbeat를 모든 세션의 runtime 확인으로 쓰지 않는다. Mac에는 adapter별 성공 관측 토큰과 제한된 observation 이벤트가 준비된 뒤 적용한다.

필수 회귀는 원격 시계 오차, 같은 초의 복수 응답, 역순 RPC, bootId 교체, 두 탭 공유, lease 만료·재확인, 선택 전환, 단순 시각 갱신 시 polling backoff 유지, snapshot gzip/body 최신성이다. 새 schema가 없는 서버에는 기존 동작으로 fallback하고 현재성 보장을 표시하지 않는다.

## M3a/M3b — 관계와 짧은 맥락

M3a는 새 API나 영구 저장 없이 전체 snapshot으로 관계 인덱스를 만든다. `sessionIdentity/buildRelationIndex/relationsFor/relationRevision`은 순수 함수로 분리한다. 현재 필터 밖의 부모도 이동할 수 있게 하되 host/agent/home/id가 같은 명시 부모만 연결한다. missing/ambiguous/cycle/self-reference는 구분한다. Claude parentSessionId의 worker/transcript namespace가 불명확하면 unresolved로 남기고 typed parentRef가 확인되기 전에는 추정 연결하지 않는다.

`app.js`의 `render/drawDetail/refreshDetail/patchDetailHead/select/prepareSessionListFor`에 관계 영역을 연결한다. 자식 변경은 부모 패널만 갱신하고 부모 transcript 재조회를 유발하지 않는다. 관계 클릭에서 launch/send는 호출하지 않는다. `goals.js`는 로드하지 않는다.

M3b의 첫 화면은 이미 읽은 선택 대화만 사용한다. 마지막 사용자 요청, 마지막 final_answer 우선 저장 답변, 명시 label/structured next, 발췌 범위·시각·원문 ref를 보여 준다. 응답 종료와 목표 완료는 별도다. 마지막 사용자 요청 뒤 답이 없으면 이전 답을 현재 요청의 결과로 붙이지 않는다.

`session_history.summarize`와 `automatic_work`의 검증된 추출 규칙만 순수 helper로 재사용한다. 저장소 sync/merge/observer는 호출하지 않고 `--auto-work`도 켜지 않는다. message/turn ID가 원문 조회 API의 bodyHash/recordOffset을 대체한다고 가정하지 않는다. 정확한 앵커가 없으면 대화 열기까지만 제공한다.

자식별 결과는 한 자식을 사용자가 펼칠 때만 읽는다. 필요할 때 `GET /api/session/context`를 추가하고 `listed_source`→기존 한 세션 read→projection 경로를 사용한다. 병렬 최대 2, 기존 본문 한도, 정확한 cache key, stale 안내, 선택 세대 확인을 둔다. 목록 밖 부모 복구는 exact resolver를 별도로 구현하기 전에는 제공하지 않는다.

관계 테스트는 동일 ID의 다른 home/host, 부모 필터 제외, 다단계·순환·중복, 목록 누락, Claude namespace, 자식 갱신, 돌아오기 초안·스크롤을 검증한다. context 테스트는 final 뒤 commentary, 새 요청 후 미답, 명시 next와 추정 질문 분리, 잘린 본문, offline·늦은 응답, 원문 앵커 부재를 포함한다.

M3의 제거는 새 script·UI 호출·선택 lazy API를 되돌리는 범위다. 원본·goals/automatic-work 데이터 변환은 없다.

## M4와 제외 범위

- `HostStream.run`에서 이미 송신되는 WATCH_PROTO/hello를 검증한다. 미지원이면 해당 live 경로만 중단하고 단발 조회/마지막 기록을 유지한다.
- Mac의 모르는 Claude state→idle, 모르는 Codex status→closed 기본값은 별도 호환성 위험이다. 지원 notLoaded와 미지원 상태를 나누는 변경은 기존 테스트·UI 매핑과 함께 독립 검토한다. 현 운영 장애로 집계하지 않는다.
- `collector.section`의 adapter 오류→빈 목록은 legitimate empty와 실패를 구분하는 source 상태 계약을 만든 뒤 마지막 성공 목록 보존을 검토한다. 호스트 전체 실패 캐시와 다른 문제다.
- `docs/architecture.md`의 재시도 간격과 Mac DB 목록 한도 설명을 실제 코드 상수와 맞춘다. 현재 RETRY_BACKOFF는 90/180/300초, Mac CODEX_THREAD_LIMIT는 home당25, Windows LIMIT는100이다.
- PC의 모든 과거 기록 상세 조회, 모든 app-server 강제 시작, native hook 전역 설치, 새 오케스트레이션 서버, diff/PR 중심의 별도 목표 시스템은 이번 첫 두 단위에서 제외한다.

## 공통 인수 검증

구현 범위: P0의 비공개 복귀 참조·exact resolver·Windows 자동 정리 보호,
M1a의 상태 합성/이유 전달, M2의 영구 질문 영수증, Windows 대상으로 한 M1b,
M3a 관계 이동과 M3b 선택 대화 발췌, M4의 adapter별 실패 보존·프로토콜·unknown
처리를 반영했다. 사용자 추가 요청인 같은 상세 영역의 채팅/터미널 전환도 포함했다.

M3는 기존 선택 대화 읽기와 원문 조회를 재사용한다. 새 context API와 모든 자식의
선조회는 추가하지 않고, 관련 작업을 눌렀을 때 그 한 대화만 연다. 자동 작업 서비스와
퇴역한 목표 UI를 켜지 않았다. Windows 지속 실행 서비스 설치와 Mac 전체 runtime
lease 확대는 이번 구현에 포함하지 않았다. 실제 원인을 확인하지 않고 별도 실행기를
설치하거나, Mac heartbeat를 세션별 실행 확인으로 사용하는 확장은 보류한다.

자동 검증 결과와 실제 기기에서 남은 인수 항목은
[검증 기록](../verification.md#2026-10-21-세션-복귀상태질문-영수증과-상세-화면)에 기록했다.
운영 배포·실제 사용자 세션 재실행·반나절 유지 시험은 완료로 간주하지 않는다.

| 시나리오 | 기대 결과 |
| --- | --- |
| 실행 중 세션 연결 | 같은 원본 ID·계정, 새 대화·자동 입력 없음 |
| 종료 대화 재개 | 같은 ID의 명시 재개, 보드에서 직접 닫은 대상은 차단 |
| 새 대화/이전 | 새 ID와 원본이 구분되고 계획의 대상·파일·환경 경계 유지 |
| 원본 ID 없음 | 유사 제목·폴더로 다른 세션을 선택하지 않음 |
| 오프라인/저장기록 | 연결 상태·마지막 관측·실행 미확인 구분 |
| 입력 접수 직후 단절 | 기존 receipt 확인, 자동 중복 전송 없음 |
| 한글 조합 중 Enter | 조합 완료 전 전송 없음 |
| 초안·첨부 중 선택 전환 | 원래 conversation identity에 보존, 다른 대상 전송 없음 |
| 열린 터미널 대상 변화 | 정확한 nativeSource·신뢰도 검증, 잘못된 작업에 입력 없음 |
| 부모/봇/일회성 결과 | 정확한 관계·참조, 모호함 구분, 원래 읽기/실행 정책 유지 |
| CLI/서버 업데이트 | bootId·CSRF·pending receipt·지원하지 않는 상태와 복원 범위 구분 |

관련 검증이 통과한 뒤 전체 release 검증은 다음을 한 번 수행한다.

```sh
python3 -m unittest discover -s tests -v
node --test tests/test_*.cjs
python3 scripts/check_public.py --release
```

실제 모델·native 기기 이전·업데이트 중 재개는 임시 전용 세션에서만 별도로 확인한다. 이 계획 작성과 합성 테스트 통과는 운영 배포나 실제 세션 재실행 완료를 뜻하지 않는다.
