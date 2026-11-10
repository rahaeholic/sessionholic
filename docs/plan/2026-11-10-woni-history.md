# 하던일 원이 창: 이전 대화·흐름 목록·대화 찾기, 담당 확인 체크 (2026-11-10)

라이언: "원이 업데이트할 때 하던일 원이도 같이 업데이트해." Ryan One 앱 1.40.0(대화 찾기·흐름 목록·이전 대화·말풍선 흐름 이름)과 1.40.1(담당 확인도 체크)을 하던일 원이 창(`web/woni.js`, `#woni-dialog`)에 맞춘다. 앞 작업 규격은 `2026-11-10-woni-panels.md`.

## 원이 서버 원본 API (읽기, 이미 있음)

- `GET /v1/state`: 대화는 최근 60건만. 대화마다 전체 목록 순번 `seq`와 지금 흐름 이름 `streamTitle`, 그리고 `chatTotal`·`chatsHasMore`.
- `GET /v1/chats?before=&stream=&through=&limit=`(1~200, 기본 40) → `{chats, hasMore, total, streamTitle}`. before 바로 앞까지 오래된 → 최신, stream이면 그 흐름만, through면 그 대화와 앞 3개까지. 400건이 넘게 멀면 `{chats: [], tooFar: true}`.
- `GET /v1/chat-search?q=&offset=&limit=`(q 1~100자·낱말 8개 이하, limit 1~50) → `{q, items:[{id, seq, kind, status, createdAt, finishedAt, stream, streamTitle, question, snippet}], total, nextOffset, streams:[{id, title, count, lastAt}](5개)}`.
- `GET /v1/streams` → `{items:[{id, title, count, running, unread, lastAt, lastId, lastText, lastAnswer, lastStatus}], total, readThrough}`(최근순).
- 대화 id `[A-Za-z0-9_-]{1,100}`, 흐름 slug `[a-z0-9-]{1,40}`. 잘못된 값은 원이 서버가 409로 거절한다.

## 하던일 서버 API (새로 만든 읽기 3개)

기존 `/api/woni/*` 읽기와 같은 로그인·`scrub_payload`·오류 규칙(400 = 확정 거절, 409 = 연결·결과 불명). 원이 서버에는 `readToken`으로 GET만 한다. 쓰기 경로는 늘리지 않았다.

- `GET /api/woni/chats` — 키는 `before`·`through`(대화 id), `stream`(slug), `limit`(1~200)만. 응답 `{chats:[대화], hasMore, total, tooFar?(true일 때만), streamTitle?}`. 대화 키는 `/api/woni`와 같고 `seq`(0 이상 정수일 때만)를 더한다.
- `GET /api/woni/chat-search` — 키는 `q`(앞뒤 공백 뺀 1~100자, 제어 문자 금지), `offset`(0~1,000,000), `limit`(1~50)만. 응답 `{q, items, total, nextOffset, streams}`에서 위에 적은 키만.
- `GET /api/woni/streams` — 쿼리 없음. 응답 `{items, total}`에서 위에 적은 키만.
- 찾은 대화의 `question`과 흐름의 `lastText`는 대화 `text`의 발췌라서 `text`처럼 `scrub_text`로 가린다(이 두 읽기에서만).
- `GET /api/woni`에 `chatsHasMore`(bool)·`chatTotal`(0 이상 정수)과 대화마다 `seq`를 넘긴다.

## 화면

- 받은 조각(이어 받은 이전 대화·연 흐름 전체·최근 60건)은 `seq`로 합친다. 같은 대화는 나중 조각이 이긴다. 60건 창에서 밀려난 끝난 대화는 이전 대화로 남긴다. 읽음·배지·추천 칩은 지금처럼 최근 60건(`latestData`)만 본다.
- 대화 목록 맨 위에 닿으면(스크롤) 앞 40건(흐름 보기면 그 흐름의 앞 100건)을 이어 받고 보던 자리를 지킨다. 맨 위 줄: 불러오는 중 · 실패와 '다시 시도'(자동 반복 없음) · '이전 대화 더 보기' · 흐름의 '이 흐름의 첫 대화 · 모두 N개'.
- 머리말 돋보기 → 찾기 화면. 검색어가 없으면 흐름 목록(이름·마지막 시각, 진행 중이면 맡긴 말·실패면 '답을 받지 못했어요 · …'·끝났으면 답 첫 줄, 진행 중 표시, 새 답 수). 있으면 0.3초 뒤 찾기: 흐름 이름 일치 → '대화 N건'(흐름 이름 또는 '먼저 알림'·'흐름 없음', 시각, '나: 질문', 답 토막, 찾은 낱말 굵게), '더 보기'.
- 찾은 대화: 받아 둔 목록에 있으면 그 자리로 스크롤해 잠깐 강조, 없으면 `through`로 이어 받아 그 자리로, `tooFar`면 그 흐름을 처음부터 열어 그 자리로(흐름이 없거나 실패하면 원문 창).
- 흐름 목록·말풍선 흐름 이름·'이 흐름만 보기'는 그 흐름을 처음부터 연다(`stream=&limit=200`). Esc·폰 뒤로가기는 흐름을 닫고, 흐름 목록에서 열었으면 목록으로(연 흐름 행에 포커스), 찾기 화면에서는 대화로 돌아간다. 닫을 것이 없을 때만 창이 닫힌다. 흐름 띠의 '전체 대화'는 대화 화면에 남아 모든 대화를 보인다(앱의 띠 닫기와 같음). 흐름을 열면 포커스는 띠의 '전체 대화'로, 찾은 대화로 가면 입력창으로 옮긴다.
- 원이 답(일하는 중·실패 포함) 아래 `# 흐름 이름`(누르면 그 흐름만). 먼저 알림·흐름 없는 대화·흐름 보기 안에서는 달지 않는다.
- 담당 확인(1.40.1): 목록 체크와 상세의 '완료로 표시'가 된다. 남은 일 목록 위 안내 "누가 할지 모르는 일이에요. …7일 뒤 보관으로 옮겨요.", 보관 안내 "녹음한 지 14일이 넘은 일, 담당 확인에서 며칠 지난 일이라 … '담당 구분 수정'으로 꺼내 주세요."
- 잠금(`lock`)과 인증 세대가 바뀌면 이어 받은 대화·흐름·찾기 결과와 늦은 응답을 모두 버린다.
