# Windows 세션 모델 선택 지원

목표: 모바일 채팅에서 Windows Codex 세션의 `/model`, `/effort`를 선택하고 다음 새 응답부터 적용할 수 있게 한다.

원인: 공통 채팅 UI와 `Chat.command`는 모델 조회를 `action=command`로 전달하지만 `Transfers.windows_rpc`와 `WindowsRuntime.chat`의 허용 목록 모두에 command가 없다. Windows 전송에도 저장된 모델 선택 적용이 없다. 모바일 화면만 바꿔서는 해결되지 않는다.

수정 파일:
- `windows_runtime.py`: command 라우팅, 실제 소유 서버의 모델 목록 조회, 세션별 모델·effort 저장, 동일 요청 중복 방지, read의 current/next 반환, idle `turn/start`에만 선택값 적용. 기존 Store와 endpoint 신원 확인을 재사용한다. 공용 config나 인증·권한을 변경하지 않는다.
- `tests/test_windows_runtime.py`: 실제 production entrypoint에서 모델 목록/선택/effort/재시작/중복 요청/잘못된 선택/진행 중 작업/다른 세션 격리/오류를 검사한다. fake native transport만 사용한다.
- `transfer.py`, `tests/test_windows_host.py`: Windows 전송 경계에서 command를 허용하고, `Chat.command`부터 SSH stdin 프로그램까지 연결되는지 검증한다. 등록되지 않은 작업과 임의 shell 명령 차단은 유지한다.
- `docs/limitations.md`, `scripts/public-files.json`, 이 계획 및 종료 리뷰: 지원 범위와 배포 대상 문서 목록을 갱신한다.
- 참조: `native_chat.py`의 기존 Mac 모델 선택 계약, `web/app.js`의 모바일 공통 선택 UI, `chat.py` 명령 라우팅. 현재 POSIX 의존 native_chat을 Windows에 통째로 이식하지 않는다.

순서: Windows command 거절을 재현 → 모델 목록·설정 저장 → 전송 적용 및 readback → 관련 Python/프런트 회귀와 공개 감사 → 셀프 리뷰 → 한 번 배포 → 운영 Windows 모델 목록의 읽기 조회 확인.

`/compact`도 공통 명령 계약에 맞춰 지원하되 idle에서만 native compact를 한 번 요청하고, 응답 유실 때 재실행하지 않는다. 모델 변경은 다음 **새 응답**에 적용하며 진행 중인 응답 설정과 터미널의 아직 보내지 않은 입력은 바꾸지 않는다. 기존 사용자 세션에는 테스트 입력·모델 변경·요약을 전송하지 않는다.
