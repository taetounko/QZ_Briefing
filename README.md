# QZ Briefing

키움 OpenAPI+ 기반 국내 증시 브리핑 전용 Python 프로젝트다. 이 프로젝트는 분석과 설명을 제공하며 자동매매, 주문, 매수, 매도 기능을 구현하지 않는다.

## 현재 상태

프로젝트 문서와 기본 골격만 구성된 초기 단계다. 실제 분석 엔진과 키움 OpenAPI+ 연결 코드는 아직 포함하지 않는다.

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

## 프로젝트 구조

- `src/qz_briefing/`: 애플리케이션 소스 패키지
- `tests/`: 테스트 패키지
- `config/`: 설정 파일 위치
- `data/`: 수집·저장 데이터 위치
- `logs/`: 실행 로그 위치
- `reports/`: 브리핑 결과 위치

세부 범위와 규칙은 `PROJECT_BRIEF.md`, `REQUIREMENTS.md`, `AGENTS.md`를 따른다.
