# 로컬 적용과 첫 GitHub 게시

이 패키지는 기존 FX 파일을 검토해 만든 업그레이드이다. Codex·배터리 메트릭은
변경하지 않는다. 기존 API 키 파일을 그대로 사용하므로 키를 다시 입력하거나
채팅/GitHub에 보낼 필요가 없다.

## 1. 적용

압축을 다운로드 폴더에 저장한 다음:

```sh
cd "$HOME/Downloads" &&
ditto -x -k RunCatFX_20261005.zip . &&
cd RunCatFX_20261005 &&
sh apply_local.sh
```

API `/quote` 접근과 직전 일봉 종가 대비 등락률을 실제 키로 사전 확인한다.
사전 확인에 실패하면 기존 작업을 중지하거나 기존 스크립트를 덮어쓰지 않는다.
키 파일 권한을 600으로 좁히는 조치는 실패해도 유지될 수 있다. 설치 중 오류는
기존 대상 파일과 작업 상태를 복구하며, 복구 실패는 별도로 보고한다.

`ALL LOCAL CHECKS PASSED`가 나오고 RunCat 카드가 갱신되면 로컬 적용 완료다.
기본 소스는 계속 `~/.runcat/usd-krw.json`이므로 재등록하지 않는다. 실제 메뉴바
폭은 RunCat이 결정하므로 숫자가 잘리는지는 화면에서 확인한다.

메뉴 예시 `₩1,344 ▲0.28%`는 배치 예시이지 현재 시세가 아니다.
기준은 Twelve Data `interval=1day`의 `previous_close`이고 KST 자정이나
프로그램 직전 실행값을 의미하지 않는다.

## 2. 첫 공개 게시

아래는 **새 공개 저장소** `Differential-3893/runcat-usd-krw`를 만드는 명령이다.
소스만 명시적으로 선택해 게시한다. 설치 폴더, API 키, 실제 시세 JSON과 로그는
업로드하지 않는다. 같은 이름의 다른 내용이 있는 저장소는 덮어쓰지 않는다.

```sh
cd "$HOME/Downloads/RunCatFX_20261005" &&
gh auth refresh -h github.com -s workflow &&
python3 -B publish_github.py --push --public --wait-ci
```

`PUBLISHED:`와 `ALL GITHUB CI CHECKS PASSED:`까지 확인한다. API 키 권한과
GitHub gh 인증은 서로 별개다. gh 인증은 Twelve Data 키를 요청하지 않는다.

이미 같은 소스를 게시했다면 `ALREADY UP TO DATE`로 건너뛰고 CI를 재확인한다.
CI 대기 실패는 이미 게시된 코드를 자동 취소하지 않는다. 위 명령을 다시 실행해
상태를 확인할 수 있다. 다른 두 RunCat 저장소는 변경하지 않는다.

## 확인과 복구

```sh
python3 -B scripts/manage_install.py verify
python3 -B scripts/manage_install.py diagnose
```

오류가 나면 **키를 보내지 말고** 이 명령의 정제된 출력과 설치 출력만 보관한다.
`Backup:`에 나온 정확한 폴더로 원복할 수 있다:

```sh
python3 -B scripts/manage_install.py rollback "/실제/출력된/백업폴더"
```

원복은 저장된 옛 파일과 시세를 복원하므로 다음 자동 갱신 전까지 예전 시세일 수
있다. 원복 명령을 단순 점검용으로 실행하지 않는다. 일반 제거는 `sh uninstall.sh`.
제거해도 API 키·시세·로그·백업은 보존된다.

이 배포물을 개발하는 환경에서는 실제 사용자의 Twelve Data 키와 macOS GUI를
검사하지 않았다. 로컬 적용 명령의 성공은 해당 Mac에서의 실제 조회와 설치를
확인하며, 전체 잠자기/깨우기·타이머 한 주기·화면 표시까지 자동 검증하진 않는다.
