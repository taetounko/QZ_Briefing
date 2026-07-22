# QZ Briefing

## 운영 일정

- Windows 대화형 사용자 세션 자동 시작: 07:30
- 장전 예상체결 감시: 08:00~09:00 (5분 간격, 08:59 최종 표본)
- 장전 최종 브리핑: 09:00 (공식 개시 신호가 없으면 09:05까지 확인)
- 장중 브리핑: 10:00
- 장마감 브리핑: 15:40
- 정상 종료: 20:00

무인 접속은 Kiwoom OpenAPI+의 공식 자동로그인 설정만 사용합니다. 아이디,
비밀번호, 인증서 입력 또는 로그인 창 클릭을 자동화하지 않습니다. 예상체결과
호가 정보는 실제 외국인·기관 수급으로 해석하지 않습니다.

## 상주 연결 런타임 상태

`QApplication` 아래에서 `QTimer`, QAx 어댑터, 연결 관리자를 묶는 상주 연결 런타임 골격과 가짜 객체 기반 단위 테스트가 구현되어 있다. 실제 OCX를 사용하는 통합 실행과 자동로그인 상태의 무인 시작은 아직 수동 검증 전이다.

키움 OpenAPI+ 기반 국내 증시 브리핑 전용 Python 프로젝트다. 이 프로젝트는 분석과 설명을 제공하며 자동매매, 주문, 매수, 매도 기능을 구현하지 않는다.

## 현재 상태

프로젝트 문서와 기본 골격, 키움 OpenAPI+ 환경·로그인 진단만 구성된 초기 단계다. 실제 분석 엔진은 아직 포함하지 않는다.

## 실행 환경

- Windows PowerShell
- Python 3.11 32-bit
- 프로젝트 루트의 `.venv` 가상환경
- 키움 OpenAPI+ 및 사용자가 로그인한 영웅문

## 가상환경 사용

Python 3.11 32-bit로 생성된 `.venv`가 준비되어 있어야 한다.

```powershell
.\.venv\Scripts\Activate.ps1
python --version
python -c "import struct; print(struct.calcsize('P') * 8)"
```

버전은 Python 3.11, 비트 수는 32로 확인되어야 한다. 환경 검증과 외부 패키지 도입은 `TASKS.md` 순서에 따라 별도 작업으로 진행한다.

## PyQt5 및 QAxWidget 환경 검사

프로젝트 루트의 Windows PowerShell에서 다음과 같이 실행한다. 이벤트 루프나 창을 실행하지 않고 빈 `QAxWidget` 생성까지만 확인한다.

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m qz_briefing.diagnostics.qax_environment_check
```

## 키움 OpenAPI+ OCX 연결상태 검사

로그인창을 띄우지 않고 키움 OpenAPI+ OCX 탑재, API 모듈 경로, 현재 연결상태 읽기까지만 검사한다.

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m qz_briefing.diagnostics.kiwoom_ocx_check
```

- Connection state `0`: OCX는 정상이나 OpenAPI 서버에는 미연결
- Connection state `1`: OpenAPI 서버 연결완료
- 상태 `0`도 이번 OCX 환경검사 자체는 성공으로 판정한다.

## 키움 OpenAPI+ 로그인 및 연결 이벤트 검사

연결상태가 `0`이면 로그인창이 나타날 수 있으며 사용자가 직접 로그인해야 한다. 최대 300초 동안 연결 이벤트를 기다리고, 이미 연결상태가 `1`이면 로그인 요청을 건너뛴다. 실행은 VS Code 터미널에서 사용자가 직접 수행한다.

```powershell
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m qz_briefing.diagnostics.kiwoom_login_check
```

- `SUCCESS`: 로그인 이벤트가 성공이고 최종 연결상태가 `1`
- `ALREADY_CONNECTED`: 실행 당시 이미 연결되어 로그인 요청을 생략
- 알려진 오류명: 로그인 이벤트가 해당 오류코드를 반환
- `TIMEOUT`: 300초 안에 로그인 이벤트를 받지 못함
- 현재 검사는 단일 로그인 요청만 수행하며 장시간 운영 중 자동 재접속은 구현하지 않는다.
- 실제 실서버 로그인 검증에서 `OnEventConnect` 오류코드 `0`과 최종 연결상태 `1`을 확인했다.
- 계정정보와 인증정보는 키움 로그인창에만 입력하며 프로그램이나 Git에 저장하지 않는다.

## 키움 연결 관리자

Qt나 실제 OCX에 의존하지 않는 연결 관리자가 실행 시 연결상태를 먼저 확인하고, 연결 중에는 주기적으로 끊김을 감지한다. 끊김이 확인되면 즉시 반복하지 않고 설정된 지연 후 제한적으로 재접속하며 기본 최대 횟수는 3회다.

자동로그인 설정 자체는 변경하지 않는다. 실제 QAxWidget 어댑터 통합과 실환경 연결 끊김 복구 검증은 후속 단계에서 진행한다.

## 키움 QAx 어댑터

`KiwoomQAxAdapter`는 `KHOPENAPI.KHOpenAPICtrl.1` 바인딩, 연결상태 읽기, 단일 연결 요청과 `OnEventConnect` 리스너 전달을 담당한다. QApplication과 이벤트 루프를 생성하지 않으며 재접속 횟수나 지연 정책도 관리하지 않는다.

어댑터 구현과 가짜 위젯 기반 단위 테스트는 완료했다. 실제 상주 런타임, QTimer 연결과 자동로그인은 아직 통합하지 않았고 실제 연결 끊김 복구 검증도 후속 작업이다.

## 프로젝트 구조

- `src/qz_briefing/`: 애플리케이션 소스 패키지
- `tests/`: 테스트 패키지
- `config/`: 설정 파일 위치
- `data/`: 수집·저장 데이터 위치
- `logs/`: 실행 로그 위치
- `reports/`: 브리핑 결과 위치

세부 범위와 규칙은 `PROJECT_BRIEF.md`, `REQUIREMENTS.md`, `AGENTS.md`를 따른다.
