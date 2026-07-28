# vision-dev-agents

멀티 에이전트 기반 자율 소프트웨어 개발 파이프라인의 초기 스캐폴드입니다.
기획자(Planner) / 개발자(Developer) / 코드 리뷰어(Reviewer) / GUI 검증(GUI Tester)
4개의 LLM 에이전트가 협력하여, `localStorage` 기반 정적 웹앱(Todo 리스트)을
자율적으로 구축하고 검증합니다.

자세한 설계 배경은 [`멀티에이전트_자동개발시스템_기획서_v2.md`](./멀티에이전트_자동개발시스템_기획서_v2.md)를 참고하세요.

> **현재 단계**: Planner(`agents/planner.py`)와 Developer(`agents/developer.py`)는
> 실제 LLM 로직으로 구현되어 있습니다. Reviewer/GUI Tester는 아직
> `Stub*Agent`로 남아 있고 `NotImplementedError`를 발생시킵니다. 다음 단계에서
> 하나씩 채웁니다.

## 폴더 구조

```
.
├── agents/                  # 에이전트별 모듈 (독립적으로 교체 가능하도록 인터페이스로 분리)
│   ├── base.py                # PlannerAgent / DeveloperAgent / ReviewerAgent / GUITesterAgent 추상 클래스
│   ├── models.py               # 에이전트 간에 오가는 공용 데이터 모델 (Phase, DevResult, ReviewResult, ...)
│   ├── schemas.py               # LLM 구조화 출력(JSON Schema) Pydantic 모델
│   ├── planner.py                # OpenAIPlannerAgent -- create_plan 구현, replan/에스컬레이션/보고서는 스텁
│   ├── developer.py               # OpenAIDeveloperAgent -- 파일 생성 + eslint 자체 검증 구현
│   ├── reviewer.py                 # StubReviewerAgent
│   └── gui_tester.py                # StubGUITesterAgent
├── orchestrator/             # 파이프라인 루프 제어
│   ├── config.py                # .env 로드 및 재시도 상한 등 설정
│   ├── state.py                  # 진행 상태를 /state에 JSON으로 저장/복원
│   ├── logging_setup.py           # 콘솔 + /logs 파일 로깅 설정 (UTF-8 강제)
│   ├── encoding.py                 # stdout/stderr UTF-8 재설정 헬퍼
│   ├── proc.py                      # UTF-8을 강제하는 서브프로세스 실행 헬퍼
│   └── pipeline.py                   # Orchestrator: 기획→개발↔리뷰↔GUI검증→재계획→에스컬레이션 루프
├── scripts/                  # 개별 에이전트를 수동으로 검증하기 위한 스모크 테스트 스크립트
│   ├── smoke_test_planner.py
│   └── smoke_test_developer.py
├── tools/
│   └── eslint.config.js       # Developer 에이전트의 app.js 자체 린트용 flat config (핵심 규칙만 사용)
├── target-app/                # 에이전트들이 생성한 Todo 앱 결과물 (index.html / style.css / app.js)
├── state/                     # 파이프라인 상태 JSON (plan.json, pipeline_state.json)
├── logs/                      # 실행 로그
├── main.py                    # 엔트리 포인트
├── .env.example                # OPENAI_API_KEY 등 환경변수 템플릿
└── pyproject.toml              # Python 3.12 기준 의존성 정의 (uv/venv 호환)
```

## 에이전트 구성

| 컴포넌트 | 역할 |
|---|---|
| Planner | 요구사항을 Phase로 분할, 성공 조건 정의, 반복 실패 시 재계획 및 사람 에스컬레이션 |
| Developer | 코드 작성, 내장 Lint/AST 툴로 문법 오류 자체 수정 |
| Reviewer | 로직/성공 조건 부합 여부에 대한 의미적 코드 리뷰 |
| GUI Tester | Set-of-marks 기반 Vision 판단으로 화면 인지 및 클릭/입력 시뮬레이션 |

각 에이전트는 `agents/base.py`의 추상 클래스를 구현하는 형태로 정의되어 있어,
추후 다른 LLM/전략으로 교체하더라도 `orchestrator/pipeline.py`는 수정할 필요가
없습니다.

## 오케스트레이션 흐름

`orchestrator/pipeline.py`의 `Orchestrator.run()`은 다음 순서로 동작합니다.

1. Planner가 요구사항을 Phase 목록(+ 성공 조건)으로 분할
2. 각 Phase에 대해 Developer → Reviewer → GUI Tester 루프를 최대
   `MAX_DEV_REVIEW_RETRIES`회까지 반복 (Reviewer/GUI Tester의 피드백을 다음
   Developer 시도에 전달)
3. 루프가 상한 횟수만큼 반복돼도 실패하면 Planner에게 재계획을 요청
   (최대 `MAX_REPLAN_ATTEMPTS`회)
4. 재계획으로도 해결되지 않으면 Planner가 사람에게 에스컬레이션하고, 사람의
   피드백을 반영해 루프를 한 번 더 재개
5. 모든 Phase가 통과하면 Planner가 최종 개발 요약 보고서를 작성

## 인코딩 규칙 (프로젝트 전역)

Windows 콘솔 코드페이지(cp949 등)로 인한 한글 깨짐을 막기 위해, 이 저장소의
**모든 모듈**은 다음을 지킵니다. 새 모듈을 추가할 때도 동일하게 적용하세요.

- 파일 read/write는 항상 `encoding="utf-8"`을 명시 (`Path.read_text` /
  `write_text`, `open()` 등)
- JSON 저장은 `ensure_ascii=False`로 저장해 한글이 유니코드 이스케이프가
  아닌 실제 문자로 저장되게 함
- 콘솔 출력이 필요한 진입점(스크립트의 `main()` 등)에서는 가장 먼저
  `orchestrator.encoding.ensure_utf8_stdio()`를 호출해 `stdout`/`stderr`를
  UTF-8로 재설정 (`orchestrator/logging_setup.py`의 `setup_logging()`은 내부에서
  이미 호출하므로, 오케스트레이터를 거치는 경로는 별도 호출 불필요)
- 로깅 파일 핸들러는 `encoding="utf-8"` 지정 (`logging_setup.py` 참고)
- 서브프로세스 호출은 `orchestrator.proc.run_utf8()`을 사용 (`encoding="utf-8"`,
  `errors="replace"`, 환경변수 `PYTHONIOENCODING=utf-8`을 자동으로 적용)

## 환경 준비

Python 3.12 기준입니다.

### uv 사용 (권장)

```bash
uv sync
cp .env.example .env   # OPENAI_API_KEY 입력
uv run python main.py
```

### venv 사용

```bash
python3.12 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env
python main.py
```

> Reviewer/GUI Tester는 아직 스텁이므로, `main.py` 실행 시 Reviewer 단계에서
> `NotImplementedError`가 발생하는 것이 정상입니다.

개별 에이전트만 따로 검증하고 싶다면 `scripts/` 아래 스모크 테스트를 사용하세요.

```bash
uv run python scripts/smoke_test_planner.py     # 샘플 요구사항 -> state/plan.json 생성
uv run python scripts/smoke_test_developer.py   # plan.json의 첫 pending Phase -> target-app/ 생성
```

## 다음 단계

1. ~~`agents/planner.py`: 요구사항 분할 + 성공 조건 정의 로직 구현~~ (완료 -- create_plan만; replan/에스컬레이션/보고서는 예정)
2. ~~`agents/developer.py`: 코드 생성 + 내장 Lint/AST 자체 수정 로직 구현~~ (완료)
3. `agents/reviewer.py`: 성공 조건 대비 의미적 리뷰 로직 구현
4. `agents/gui_tester.py`: Set-of-marks 탐지 + Vision 기반 액션 판단 + OS 레벨 입력 제어 구현
5. `agents/planner.py`의 `replan` / `request_human_escalation` / `summarize_report` 구현
