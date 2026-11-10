# 하던일 원이 창: 할 일·기록(녹취노트)·디자인 정리 (2026-11-10)

라이언 2026-11-10 09:20: "하던일에서 원이 쓸 때 다른 기능들도 일부 좀 필요한거같아 라이언원 앱의 기능들. 예를들어 할일, 시킨일 등. 기록 노트 원문 정도는 볼 필요도 있고. 그리고 UIUX와 디자인이 좀 더 세련되면 좋겠어 지금은 뭔가 살짝 허접함."

Ryan One 앱(안드로이드, 저장소 ~/Personal/ryan-recall-woni, 읽기 전용 참고)의 원이 화면에 있는 기능 중 세 가지를 하던일 원이 창(`web/woni.js`, `#woni-dialog`)에 옮긴다.

1. **할 일**: 내가 할 일(mine) · 내가 맡긴 일(delegated) · 담당 확인(unassigned). 남은 일/완료/보관 보기, 완료 체크·되돌리기·완료 보고 받음.
2. **기록**: 정리된 녹취노트 목록·검색, 노트 본문 읽기, 녹취 원문(전사 구간) 보기·검색.
3. **디자인**: 원이 창 전체(머리말·탭·대화·입력·작업 현황·새 화면)를 DESIGN.md 체계 안에서 더 세련되게.

담당 구분 수정(task-owner), 관련 작업 연결(link), 노트를 참조로 붙여 묻기는 이번 범위가 아니다(앱에서 한다).

## 원이 서버(Air15, 127.0.0.1:18772) 원본 API — 이미 있음

`woni_bridge.local_call`이 이미 쓰는 방식 그대로(GET은 `readToken`, 쓰기는 `token` + `X-Woni: 1`, 항상 `X-Woni-Task-Roles: 1`).

- `GET /v1/state` → 할 일 관련 키: `actions`(남은 일, 최대 수백), `completedActions`(최근 150), `archivedActions`(최근 150), `actionCount`, `delegatedCount`, `unassignedCount`, `followupCount`, `dueCount`, `autoDoneCount`, `completedActionCount`, `archivedCount`, `syncedAt`, `driveError`. 그 밖의 키(chats, sessions, working, reviews, attention, decisions…)는 할 일 응답에 넣지 않는다.
  - 할 일 한 건의 원본 키: `archived, assignee, attention, autoBasis{quote,source,at,reason}, candidates, counterpart, due, guessBasis, guessName, id(24자 16진), kind(mine|delegated|unassigned), linkedSession, ownerGuess, previousStatus, project, quote, recordSummary, recordTitle, recordedAt, reportQuote, requestQuote, requestedBy, source, sourceId, stateEvent(빈 문자열 가능, 예 'xxxx:completion-reported'), status, taskStatus(open|reported|done), title, url`.
- `GET /v1/notes?q=&offset=` → `{items:[{id,title,recordedAt,preview}], total, query, nextOffset(int|null), syncedAt, error}`. 한 번에 40개.
- `GET /v1/note?id=&offset=` → `{id,title,recordedAt,text(마크다운풍 본문),nextOffset,revision,url(Drive 링크),actions,currentActions,summary{…},decisions,audio{…}}`.
- `GET /v1/transcript?id=&offset=` → 전체 원문 페이지 `{sourceId,text,truncated,offset,nextOffset,revision,label,audio}`.
- `GET /v1/transcript?id=&format=passages&offset=&q=` → `{sourceId,passages:[{id,kind,label,text,position,company,project,segmentId,start,end,speaker,scopeBasis,transcriptDoubt}],total,nextOffset,query,label,revision,audio}`.
- `GET /v1/transcript?id=&evidenceId=&context=1` → 구간 하나 + `context:[구간…]`, `sourceId`, `revision`.
  - Slack에서 온 기록(id가 `slack-`로 시작)은 원문이 없어 409와 `{error: "…"}`를 돌려준다.
- `POST /v1/task-status` 본문 `{actionId, status, expectedEvent, requestId}` → `{actionId, taskStatus, stateEvent, result("saved"|"unconfirmed"), …}`. 정확한 반환 키는 ~/Personal/ryan-recall-woni/worker/recall/woni.py `def task_status`를 읽어 확인한다. 같은 requestId 재시도는 첫 결과를 돌려준다. 상태가 다른 곳에서 바뀌었으면 ValueError 메시지(400).

## 하던일 서버 API (새로 만든다) — 백엔드와 화면이 함께 지키는 규격

모두 기존 `/api/woni*`와 같은 로그인·CSRF·`scrub_payload`를 거친다. 오류는 기존 원이 경로와 같은 방식(`{error}` + 상태 코드)으로.

- 상태 코드(구현): **400 = 요청이 확정적으로 거절됨**(검사 실패, 원이 서버의 409·4xx 메시지 — Slack 기록 원문 없음, 상태 충돌 포함. 원이의 409도 여기서는 400으로 온다). **409 = 연결·결과 불명**(원이 연결 실패, 읽을 수 없는 응답, task-status가 원이 5xx나 시간 초과를 만난 경우 — 저장됐을 수도 있다). `task-status`가 409면 성공·실패로 단정하지 않는다. 401은 로그인, 403은 CSRF.
- `error`·`scrub_payload` 대상 키(text·title·error 등)는 기존과 같다. `/api/woni/tasks`·`/notes`의 `error`는 원이의 Drive 동기화 문구(데이터)이며 HTTP 오류가 아니다.

- `GET /api/woni/tasks` →
  ```
  { "actions": [Task], "completedActions": [Task], "archivedActions": [Task],
    "counts": { "mine", "delegated", "unassigned", "followup", "due", "autoDone", "completed", "archived" (모두 int) },
    "syncedAt": number|null, "error": "" }
  ```
  Task = 원본에서 `id, kind, title, project, due, assignee, attention, taskStatus, previousStatus, stateEvent, quote, requestQuote, reportQuote, recordTitle, recordedAt, counterpart, sourceId, guessBasis, ownerGuess, archived`만, 그리고 `autoBasis`는 `{quote, source, at, reason}`만. `candidates`, `linkedSession`, `recordSummary`, `url`, `requestedBy`, `guessName`, `source` 등은 넣지 않는다. 없는 키는 빼고 추측해 채우지 않는다.
- `GET /api/woni/notes?q=<300자 이하>&offset=<0 이상 정수>` → `{items:[{id,title,recordedAt,preview}], total, nextOffset, query, error}`.
- `GET /api/woni/note?id=<기록 ID>&offset=<정수>` → `{id, title, recordedAt, text, nextOffset, revision, url}`.
- `GET /api/woni/transcript?id=<기록 ID>&offset=<정수>` → `{sourceId, text, truncated, offset, nextOffset, revision, label}`.
- `GET /api/woni/transcript?id=&format=passages&q=&offset=` → `{sourceId, passages:[{id, kind, label, text, start, end, speaker, position, transcriptDoubt}], total, nextOffset, query, label, revision}`.
- `GET /api/woni/transcript?id=&evidenceId=&context=1` → `{id, kind, label, text, start, end, speaker, transcriptDoubt, context:[같은 구간 키], sourceId, revision}`.
- `POST /api/woni/task-status` 본문 정확히 `{actionId, status, expectedEvent, requestId}` → `{actionId, taskStatus, stateEvent, result}`.
  - actionId `[A-Za-z0-9_-]{1,100}`, status `done|open|reported`, expectedEvent 300자 이하 문자열(빈 문자열 허용, 제어 문자 금지), requestId `[A-Za-z0-9_-]{8,100}`.
- 기록 ID 검사: `[A-Za-z0-9_.:-]{1,200}`. evidenceId `[A-Za-z0-9_-]{1,100}`. context `0|1|2`. offset 0~1,000,000. 허용하지 않은 query 키가 있으면 거절.
- `audio`(Drive 세션 ID·링크)는 어떤 응답에도 넣지 않는다.

## 화면 (web)

- 원이 창 탭: **대화 · 할 일 · 기록 · 작업**. 할 일 탭에는 `counts.due + counts.followup`가 0보다 크면 작은 숫자 배지.
- 할 일: 위에 종류 전환(내가 할 일 N · 내가 맡긴 일 N · 담당 확인 N), 보기 전환(남은 일 · 완료 · 보관 N — 보관은 0보다 클 때만), 찾기(제목·프로젝트·기록 제목·담당자, 화면에서 거름).
  - 행: 동그란 체크(담당 확인은 비활성), 제목, 맡긴 일은 `담당: {assignee 또는 '담당 미정'} · {attention}`(attention에 '확인 필요'가 있으면 강조색), 담당 확인은 guessBasis가 있을 때 `attention`에서 '담당 확인 · ' 앞부분을 뺀 글, 완료이고 autoBasis.quote가 있으면 `원이 확인 · “인용”`(2줄), 마지막 줄 `project · due`. 저장 중이면 `저장 중…`.
  - 행을 누르면 원이 창 안에서 상세로(뒤로 버튼으로 목록 복귀, 스크롤 위치 유지): 제목, `project · due · 완료/남은 일`, autoBasis 상자('원이가 끝낸 근거를 확인해 완료로 체크했어요' + 인용 + source + '틀렸으면 아래 '완료 되돌리기'를 눌러 주세요. 되돌린 일은 다시 자동으로 체크하지 않아요.'), 맡긴 일이면 담당·attention·reportQuote와 남은 일일 때 '완료 보고 받음'(status reported), 주 버튼('완료로 표시' / 맡긴 일은 '결과 확인 완료' / 완료면 '완료 되돌리기'), taskStatus가 reported면 '다시 기다리는 중으로'(open), 근거(quote, '요청 근거: ' requestQuote, recordTitle · recordedAt · counterpart), '노트 읽기'(sourceId로 기록 화면의 노트를 열고 뒤로 가면 할 일 상세로).
  - 되돌리기 상태: 맡긴 일이고 previousStatus가 reported면 `reported`, 아니면 `open`.
  - 안내 문구(앱과 동일): 맡긴 일·남은 일 → `확인 필요 {followup}개 · 완료 보고와 마감이 된 일을 먼저 보여줘요. 홀릭이·뷰링이 같은 에이전트에게 시킨 일은 넣지 않아요.` / 완료 보기이고 autoDone>0 → `'원이 확인' 표시는 끝낸 근거(Slack 글·보낸 메일·원이가 한 일)를 원이가 확인해 체크한 일이에요. 틀리면 열어서 되돌려 주세요.` / 보관 → `녹음한 지 14일이 넘고 마감이 없거나 지난 일이라 목록과 알림에서 뺐어요.` 빈 목록: 내 일 '라이언이 직접 실행하는 일이 모여요.', 맡긴 일 '다른 사람이 실행하고 라이언이 확인하는 일이 모여요.', 담당 확인 '담당자를 확정할 근거가 부족한 일이 모여요.'
  - 상태 변경: 같은 변경을 다시 누르면 같은 requestId(sessionStorage에 actionId별 {payload, id}), 성공하면 그 항목을 갱신하고 목록을 다시 받는다. 알림(토스트): `‘제목24자’ 완료` / 맡긴 일 `‘…’ 결과 확인 완료` / reported `‘…’ 완료 보고 · 결과 확인 필요` / open `‘…’ 남은 일로 되돌렸어요`. result가 unconfirmed면 `이전 변경은 저장을 확인하지 못했어요. 현재 상태를 불러왔으니 필요하면 다시 눌러 주세요.` 연결 실패는 성공·실패로 단정하지 않고 `저장 여부를 확인하지 못했어요. 다시 누르면 같은 요청으로 확인해요.` 저장 중에는 그 항목 조작을 막는다.
  - 할 일은 탭을 열 때와 열려 있는 동안 30초마다, 원이 창을 열 때 한 번(배지) 받는다.
- 기록: 찾기 입력(250ms 늦춰 요청), `정리 노트 {total}개`, 카드(날짜 작게 · 제목 2줄 · 미리보기 2줄), '노트 더 보기'(nextOffset).
  - 노트: 뒤로, 제목, 날짜, 'Drive에서 열기'(url이 있을 때, 새 탭, rel=noopener), 본문은 `MessageFormat.render`(대화의 원이 답과 같은 렌더러), nextOffset이 있으면 '이어 읽기'(revision이 바뀌면 `읽는 사이 노트가 변경됐어요. 새로고침해 주세요.`).
  - '녹취 원문 보기': passages 형식으로 받아 구간마다 화자 · `분:초`(start) · 글, transcriptDoubt면 작은 '전사 불확실'. 위에 서버가 준 label(예: 전사 원문)을 캡션으로. 원문 안 찾기(q), '더 보기'. 원문이 없거나 409면 서버 메시지를 그대로 보여 준다(Slack 기록 등).
- 기존 대화 기능·테스트(답글·추천 칩·첨부·읽음·잠금·호스트 이동)는 그대로 지킨다. 잠금(`lock`)과 인증 세대(`authEpoch`)가 바뀌면 새 화면의 상태와 늦게 온 응답을 모두 버린다.
- 디자인: DESIGN.md 토큰·형태·포커스·움직임 규칙 안에서. 밝은·어두운 테마, 폰(390px)·패드·PC, 44px 누름 영역, `prefers-reduced-motion`.
