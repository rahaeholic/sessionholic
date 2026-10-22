# 모바일 새 실행은 세션 목록에서 시작

목표: 휴대폰에서 앱을 새로 열면 이전 대화/터미널을 자동 표시하지 않고 목록에서 시작하며, 목록에서 연 대화의 뒤로가기는 앱 내부 목록으로 돌아간다. 일반 백그라운드 복귀와 데스크톱의 저장된 창 복원은 유지한다.

확인된 원인: workspace.js:changed가 인증 후 화면 크기와 관계없이 localStorage의 저장된 pane 참조를 복원한다. 활성 pane이 iframe이면 부모 문서가 목록 history에 머물러도 세션이 화면에 보일 수 있다. 앱 재활성화는 visibility/pageshow만 처리하며 앱 아이콘의 명시적 새 실행은 구분하지 않는다. backToList/backFromTerminal은 상세 entry에 앱 내부의 이전 history가 실제로 있는지 확인하지 않고 back을 부른다.

가설: 모바일 첫 문서의 자동 복원을 건너뛰고 앱 launch intent만 별도로 목록으로 보내면 이전 세션 자동 표시를 막을 수 있다. 상세 entry의 depth가 없거나 0이면 브라우저 back 대신 목록으로 전환하고, 새 상세 이동 전에 목록 anchor를 보장하면 빈 history에서 앱이 닫히는 경로를 줄일 수 있다.

변경 순서와 파일:
1. tests/test_workspace.cjs: 모바일의 저장된 단일/분할/터미널 참조가 시작 시 복원되지 않는 실패를 만든다. 데스크톱 복원은 기존 검사로 유지한다. web/workspace.js:changed에 startOnList hook을 적용하고, 이미 열린 분할 창을 제거하지 않고 부모 목록을 보이는 showList를 추가한다.
2. tests/test_frontend.cjs: 모바일 앱 launch callback, 새 상세 이동의 목록 anchor, depth 없는 상세에서 뒤로가기, 알림 링크, 일반 visibility 복귀, iframe/데스크톱 제외를 검증한다. web/app.js에서 모바일 root의 launch intent만 목록으로 처리한다. 초안 저장과 IME 확정을 먼저 하고 UI를 전환하며, terminal close/send/resume은 호출하지 않는다. native 입력/전송 영수증은 그대로 둔다.
3. web/workspace.css: 모바일 목록은 분할 창 위에서도 가로폭 전체를 쓴다. web/manifest.webmanifest: launch_handler.client_mode=focus-existing으로 지원 브라우저의 기존 페이지 새 실행을 launchQueue로 받는다. unsupported 브라우저의 cold load도 1번 경로로 목록에서 시작한다. 일반 앱 전환을 시간 간격으로 새 실행이라고 추정하지 않는다.
4. README/공개 허용 목록 갱신 후 전체 JS, manifest/instance 관련 Python, 공개 감사/diff check. 격리 canary 뒤 배포 한 번과 제공 자산 해시·manifest·기존 터미널 보존을 확인한다. 갤럭시 실기기 UI 검증은 수행한 것처럼 보고하지 않는다.

참고: https://developer.chrome.com/docs/web-platform/launch-handler · https://developer.android.com/reference/androidx/browser/trusted/LaunchHandlerClientMode
