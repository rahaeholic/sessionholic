# 창 번호로 직접 이동

목표: Chrome 기본 탭 전환과 충돌하는 Command+Option+좌우 방향키 대신, 분할 창의 표시 번호를 Option+1~4로 직접 선택한다. 한글 조합 중에도 명확한 물리 키 입력은 원래 작성창에서 조합을 확정한 뒤 이동한다.

확인:
- Google Chrome 공식 Mac 단축키 목록에 Command+Option+좌우 방향키가 브라우저 탭 이동으로 명시돼 있다. 앞선 VM 테스트는 키가 웹페이지에 이미 도착한 뒤만 검증했으며 실제 브라우저 키 수신을 검증하지 못했다. 현재 승인된 개인 Chrome 연결은 0개여서 실제 사용자 키 수신은 확인하지 못한다.
- 기존 shortcut의 isComposing/keyCode 229 일괄 거부는 focus의 commitComposition 경로에 도달하지 못하게 한다. 새 번호 조합은 실제 Digit1~4 코드가 명확한 경우에만 이 경로에 들어간다. 일반 IME/문자·후보 선택은 그대로 둔다.

파일 매핑:
- `web/workspace.js`: 새 Option+숫자 직접 선택, Command+Option 방향키 별칭 제거, window capture에서 자식/부모 키를 처리, 상단 버튼에 같은 키 표시 및 접근성 속성. 기존 Control+Command 설정 조합 유지. 원래 input·마이크·iframe은 유지한다.
- `web/index.html`: 새 기본 키와 추가 키의 도움말 구분.
- `tests/test_workspace.cjs`: 번호 선택·원시/공통 편집기·실제 capture 경로·조합 확정 지연·single/dialog/오입력 거부 경계. 기존 reserved 조합 테스트의 기대값은 더 이상 처리하지 않는 정책으로 변경한다.
- `README.md`, `docs/limitations.md`, 이 계획 및 `scripts/public-files.json`: 지원 범위와 실기 미검증 명시.
- 서버/터미널 전송/CLI·권한은 변경하지 않는다.

검증 순서: 실패하는 번호 조합·조합 중 전환 fixture를 먼저 확인하고 구현한다. 관련 workspace와 전체 JS, 공개 파일 감사·diff를 확인한다. 격리 배포 환경에서 해당 키 경로·상단 표시가 제공되는지 확인한 뒤 한 번 배포하고 모든 기존 터미널·설정·파일 해시를 검증한다. 브라우저 키 수신을 직접 확인할 수 없는 제약을 코드 테스트 통과와 혼동하지 않는다.

참고: https://support.google.com/chrome/answer/157179?hl=en
