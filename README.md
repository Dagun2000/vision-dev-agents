# vision-dev-agents

A multi-agent autonomous software development pipeline. Four LLM agents —
Planner, Developer, Reviewer, and a **Vision-based GUI Tester** — cooperate to
build and verify a `localStorage`-backed static web app end to end, with no
human in the loop unless the pipeline gets stuck.

**[한국어](#한국어)** | English

---

## English

### What makes this different

Most agentic "build an app" demos verify their own work by reading the DOM,
calling browser APIs, or running Playwright/Selenium scripts the Developer
wrote. This project deliberately doesn't: the **GUI Tester agent drives the
real screen** — real screenshots via PyAutoGUI, real mouse clicks, real
keyboard input — and a vision-capable LLM decides what to click next purely
by *looking at a labeled screenshot*, the same way a human tester would. The
execution layer (`agents/gui/execution.py`) never imports anything
DOM/browser-API-flavored; judgment (`agents/gui/judgment.py`) never touches
anything but a PNG and the Phase's success criteria.

### Architecture

| Agent | Role |
|---|---|
| **Planner** | Splits a requirement into ordered Phases with concrete success criteria; replans a Phase that keeps failing (split it smaller, or route around the failure); escalates to a human as a last resort |
| **Developer** | Writes `index.html` / `style.css` / `app.js`, accumulating each Phase on top of the last; self-lints with eslint (falls back to `node --check`) and retries on its own before handing off |
| **Reviewer** | Judges the Developer's code against the Phase's success criteria alone — deliberately never sees the Developer's own rationale — and requests a rewrite on rejection |
| **GUI Tester** | Serves the app locally, opens it in a real browser window, and loops **screenshot → Set-of-marks detection → Vision judgment → click/type → re-screenshot** until the criteria are met or the step budget runs out |

Each agent implements an ABC in `agents/base.py`, so any of them can be
swapped for a different model/strategy without touching the orchestrator.

### Multi-provider LLM support

All four agents go through `agents/llm_client.py`'s `structured_completion()`
instead of talking to an SDK directly. One `.env` setting, `LLM_PROVIDER`,
switches every agent at once between `openai` (default) / `anthropic` /
`gemini` / `ollama` — each provider's structured-output mechanism is hidden
behind the same call:

- **openai**: Responses API (`client.responses.parse(text_format=...)`)
- **anthropic**: Messages API, structured output via a forced tool call
- **gemini**: `generate_content()` with a Pydantic `response_schema`
- **ollama**: `chat()` with a JSON-schema `format`, against a local server
  (`OLLAMA_BASE_URL`, default `http://localhost:11434`)

Per-agent model names (`PLANNER_MODEL` / `DEVELOPER_MODEL` /
`REVIEWER_MODEL` / `GUI_TESTER_MODEL`) still exist separately and are
interpreted against whichever provider is active — different agents can
reasonably use different model sizes on the same provider. GUI Tester's
model additionally needs vision support, since its judgment call sends an
image (all four providers above support that).

### Long-term memory across runs (`state/lessons.md`)

Separate from `state/plan.json` (one run's state), `state/lessons.md`
persists across every run. Whenever a replanned Phase actually goes on to
pass, the Planner appends a structured entry — what was tried, why it
failed, what worked instead — and every subsequent `create_plan()` call
prepends the file's contents to its prompt as "lessons from past runs," so
mistakes don't just get fixed once, they inform future plans too.
Deliberately best-effort: `load_lessons()`/`append_lesson()` swallow their
own failures (missing file, write error, anything) and never affect the
pipeline's success/failure — this feature existing, or not, changes nothing
else about how a run behaves.

### Current status: web apps only

`agents/models.py`'s `LaunchType` has three values (`STATIC_WEB_SERVER`,
`NATIVE_EXE`, `ELECTRON_APP`), and the dashboard's app-type selector lists
all three — but only `STATIC_WEB_SERVER` is actually implemented.
`agents/gui_tester.py`'s `launch_app()` dispatcher raises
`NotImplementedError` for the other two; they're placeholders for
generalizing the GUI Tester's execution layer beyond static web apps later.
Picking "Native EXE" or "Electron" in the dashboard just declines to start a
run instead of silently doing nothing.

### How a run flows

`orchestrator/plan_pipeline.py`'s `PlanDrivenPipeline` is the live
orchestrator (there's an older `orchestrator/pipeline.py` design predating
the plan.json-driven approach — it's unused dead code, kept only for
reference). For each Phase, in order:

1. **Developer** implements it (on top of everything built so far).
2. **Reviewer** checks it against the Phase's success criteria; on
   rejection, Developer rewrites and Reviewer checks again (bounded retries).
3. **GUI Tester** drives the real app and checks the same criteria visually;
   on failure it hands its symptom description back to the Developer for a
   targeted rewrite, then re-verifies (bounded retries).
4. If a Phase exhausts its retry budget, the **Planner replans** it — split
   into smaller Phases, or a different approach — with explicit instruction
   not to just repeat the same failing strategy.
5. If replanning doesn't resolve it either, a **human is asked** (retry /
   give direction / skip / abort).
6. Once every Phase passes, a Markdown development report is written
   incrementally to `logs/` (one update per Phase, so a crash mid-run still
   leaves a record of everything completed).

### Two ways to run it

**Chat dashboard (recommended)** — a Streamlit UI that runs the pipeline in
the background and streams progress as chat messages, including prompting
you in-chat if a human escalation is needed:

```bash
uv run streamlit run dashboard.py
```

**Plain CLI** — reads the requirement from `config/requirement.txt` and runs
start to finish, asking on the console if it needs you:

```bash
uv run python main.py
```

Either way, **the GUI Tester takes over your real mouse and keyboard** while
it verifies a Phase — don't use the machine while it's running.

### Setup

Requires Python 3.12.

```bash
uv sync
cp .env.example .env   # pick LLM_PROVIDER, fill in that provider's API key
```

Key `.env` knobs: `LLM_PROVIDER` (openai/anthropic/gemini/ollama, all four
agents at once), `PLANNER_MODEL` / `DEVELOPER_MODEL` / `REVIEWER_MODEL` /
`GUI_TESTER_MODEL` (per-agent model override), `MAX_DEV_REVIEW_RETRIES`,
`MAX_GUI_TEST_RETRIES`, `MAX_REPLAN_ATTEMPTS`, and `DEBUG_INJECT_BUG` /
`DEBUG_INJECT_PHASE_ID` (see below).

### Project layout

```
.
├── agents/
│   ├── base.py            # ABC interfaces (swap any agent's implementation freely)
│   ├── models.py           # Shared dataclasses: Phase, DevResult, ReviewResult, GUITestResult, ...
│   ├── schemas.py           # Pydantic schemas for each agent's structured LLM output
│   ├── planner.py             # create_plan / replan / request_human_escalation / incremental report / lessons.md
│   ├── developer.py             # Code generation + self-lint retry loop
│   ├── reviewer.py                # Semantic review + rewrite-request loop
│   ├── gui_tester.py                # Launch dispatch + capture/judge/act/verify loop
│   ├── report.py                     # Pure Markdown rendering for the development report
│   ├── llm_client.py                  # Shared multi-provider structured-output call, used by all 4 agents
│   └── gui/
│       ├── execution.py                 # OS-level only: screenshots, clicks, keystrokes, window mgmt
│       ├── detection.py                  # Set-of-marks: stroke/fill container detection + labeling
│       ├── judgment.py                    # Builds the vision judgment prompt, calls agents/llm_client.py
│       └── server.py                       # Local static file server for target-app/
├── orchestrator/
│   ├── config.py            # .env-backed PipelineConfig
│   ├── plan_pipeline.py       # PlanDrivenPipeline -- the live orchestrator
│   ├── pipeline.py              # Old in-memory Orchestrator design -- unused, kept for reference
│   ├── logging_setup.py           # Console (terse) + file (verbose) logging, UTF-8-safe
│   ├── encoding.py                  # stdout/stderr UTF-8 reconfiguration helper
│   └── proc.py                        # UTF-8-safe subprocess wrapper
├── debug/bug_injector.py    # Deliberately sabotages app.js to demo the GUI-failure -> rewrite loop
├── scripts/                 # Smoke tests for individual agents/subsystems
├── tools/eslint.config.js   # Flat-config eslint used by the Developer's self-lint step
├── config/requirement.txt   # The requirement main.py reads
├── target-app/              # Generated app output (index.html/style.css/app.js) -- not source, regenerated every run
├── state/plan.json          # Single source of truth for every Phase's status (one run)
├── state/lessons.md         # Long-term memory across runs (see above) -- not one run's state
├── logs/                    # Run logs, incremental Markdown reports, GUI Tester screenshots
├── dashboard.py              # Streamlit chat UI entry point
└── main.py                   # Plain CLI entry point
```

### Testing individual agents

Each agent was built and can be exercised standalone via `scripts/`:

```bash
uv run python scripts/smoke_test_planner.py       # requirement -> state/plan.json
uv run python scripts/smoke_test_developer.py     # first pending Phase -> target-app/
uv run python scripts/smoke_test_reviewer.py      # review a dev_done Phase
uv run python scripts/smoke_test_gui_detection.py # Set-of-marks detection on a live screenshot
uv run python scripts/smoke_test_gui_judgment.py  # one Vision judgment call
uv run python scripts/smoke_test_gui_verify.py    # full capture/judge/act loop for one Phase
uv run python scripts/smoke_test_plan_pipeline.py # replan + human-escalation path (needs DEBUG_INJECT_BUG=true)
uv run python scripts/reset_local_storage.py      # clear target-app's localStorage without a full run
```

Any script that calls `PlanDrivenPipeline.run_phase()` /
`run_phase_with_replanning()` directly must wrap it with
`planner.start_report()` / `finalize_report()` and `gui_tester.cleanup()` --
see the existing scripts for the pattern.

### Demoing the replan/rewrite loop

`debug/bug_injector.py`, gated by `.env`'s `DEBUG_INJECT_BUG` /
`DEBUG_INJECT_PHASE_ID`, skips the Reviewer for one matching Phase and
sabotages the Developer's `app.js` before GUI verification, so the GUI
Tester catches a real (if staged) bug and the Developer→GUI Tester rewrite
loop runs for real. `DEBUG_INJECT_PHASE_ID` accepts a comma-separated list
of candidate ids (e.g. `phase-1,phase-2`) since which Phase actually
implements the add-Todo flow the injector targets varies by run.
To also force it as far as **replanning** (not just a rewrite), temporarily
lower `MAX_GUI_TEST_RETRIES` to `1` in `.env` — otherwise the first rewrite
usually just fixes the injected bug before the retry budget runs out.

### UTF-8, everywhere

Every module in this repo assumes Windows console encoding will otherwise
mangle Korean/Unicode text, and works around it explicitly:

- All file I/O passes `encoding="utf-8"` explicitly.
- JSON is written with `ensure_ascii=False`.
- Console entry points call `orchestrator.encoding.ensure_utf8_stdio()`
  (already done inside `setup_logging()` for anything going through the
  orchestrator).
- Subprocess calls go through `orchestrator.proc.run_utf8()`.

Apply the same conventions in any new module.

### License

MIT — see [`LICENSE`](./LICENSE).

Full original design doc (Korean):
[`멀티에이전트_자동개발시스템_기획서_v2.md`](./멀티에이전트_자동개발시스템_기획서_v2.md).

---

## 한국어

### 이 프로젝트의 차별점

대부분의 "AI가 앱을 만든다" 데모는 DOM을 읽거나 브라우저 API를 호출하거나
Developer가 직접 짠 Playwright/Selenium 스크립트로 자기 결과물을 검증합니다.
이 프로젝트는 의도적으로 그렇게 하지 않습니다 — **GUI Tester 에이전트가 실제
화면을 직접 조작**합니다. PyAutoGUI로 실제 스크린샷을 찍고, 실제 마우스를
클릭하고, 실제 키보드를 입력합니다. 그리고 Vision 모델이 사람 테스터처럼
**라벨링된 스크린샷을 눈으로 보고** 다음에 뭘 클릭할지 판단합니다. 실행
계층(`agents/gui/execution.py`)은 DOM/브라우저 API와 관련된 어떤 것도 import
하지 않고, 판단 계층(`agents/gui/judgment.py`)은 PNG 이미지와 Phase의
성공 조건 외에는 아무것도 보지 않습니다.

### 에이전트 구성

| 에이전트 | 역할 |
|---|---|
| **Planner** | 요구사항을 구체적인 성공 조건을 가진 순서 있는 Phase들로 분할. 반복 실패하는 Phase는 더 작게 쪼개거나 다른 접근으로 재계획. 최후에는 사람에게 에스컬레이션 |
| **Developer** | `index.html` / `style.css` / `app.js`를 이전 Phase 위에 이어서 작성. eslint로 자체 린트하고(안 되면 `node --check`로 폴백) 스스로 재시도 |
| **Reviewer** | Developer의 코드가 Phase의 성공 조건을 충족하는지만 판단 — Developer 자신의 의도/설명은 의도적으로 전달받지 않음. 거부 시 재작성 요청 |
| **GUI Tester** | 앱을 로컬에서 띄우고 실제 브라우저 창을 열어서, **스크린샷 → Set-of-marks 탐지 → Vision 판단 → 클릭/입력 → 재스크린샷** 루프를 조건이 충족되거나 스텝 예산이 소진될 때까지 반복 |

각 에이전트는 `agents/base.py`의 추상 클래스를 구현하는 형태라, 다른
모델/전략으로 교체해도 오케스트레이터를 건드릴 필요가 없습니다.

### 멀티 프로바이더 LLM 지원

4개 에이전트 전부 SDK를 직접 호출하지 않고 `agents/llm_client.py`의
`structured_completion()`을 거칩니다. `.env` 설정 하나(`LLM_PROVIDER`)로
4개 에이전트가 한 번에 `openai`(기본값) / `anthropic` / `gemini` / `ollama`
사이를 오갑니다 — 각 provider의 구조화 출력 방식은 이 하나의 호출 뒤에
숨겨져 있습니다:

- **openai**: Responses API (`client.responses.parse(text_format=...)`)
- **anthropic**: Messages API, 강제 tool call로 구조화 출력
- **gemini**: `generate_content()`에 Pydantic `response_schema` 직접 전달
- **ollama**: `chat()`에 JSON 스키마 `format` 전달, 로컬 서버로 호출
  (`OLLAMA_BASE_URL`, 기본값 `http://localhost:11434`)

에이전트별 모델 이름(`PLANNER_MODEL` / `DEVELOPER_MODEL` / `REVIEWER_MODEL`
/ `GUI_TESTER_MODEL`)은 그대로 따로 있고, 지금 활성화된 provider 기준으로
해석됩니다 — 같은 provider 안에서도 에이전트마다 다른 모델 크기를 쓸 수
있습니다. GUI Tester의 모델은 판단 호출에 이미지를 같이 보내므로 비전
지원이 추가로 필요합니다 (위 4개 provider 모두 지원함).

### 실행 간 장기 기억 (`state/lessons.md`)

`state/plan.json`(한 번의 실행 상태)과는 별개로, `state/lessons.md`는
여러 실행에 걸쳐 계속 유지됩니다. 재계획된 Phase가 실제로 통과하면
Planner가 "무엇을 시도했다가 왜 실패했고 대신 뭐가 통했는지"를 구조화된
형태로 이 파일에 추가하고, 이후 모든 `create_plan()` 호출은 이 파일
내용을 "과거 실행에서 배운 것" 섹션으로 프롬프트 맨 앞에 붙입니다 —
같은 실수를 그때만 고치고 마는 게 아니라 이후 계획에도 반영되게 합니다.
철저히 best-effort로 설계되어 있습니다: `load_lessons()`/`append_lesson()`
은 (파일 없음, 쓰기 실패 등) 어떤 예외든 자체적으로 삼키고 파이프라인의
성공/실패에 절대 영향을 주지 않습니다 — 이 기능이 있든 없든 실행 자체의
동작은 달라지지 않습니다.

### 현재 상태: 웹앱만 구현됨

`agents/models.py`의 `LaunchType`에는 값이 3개(`STATIC_WEB_SERVER`,
`NATIVE_EXE`, `ELECTRON_APP`) 있고 대시보드의 앱 타입 선택지도 셋 다
보여주지만, 실제로 구현된 건 `STATIC_WEB_SERVER`뿐입니다.
`agents/gui_tester.py`의 `launch_app()` 디스패처는 나머지 둘에 대해
`NotImplementedError`를 던집니다 — 나중에 GUI Tester의 실행 계층을 정적
웹앱 이외로 일반화하기 위한 자리만 잡아둔 상태입니다. 대시보드에서
"Native EXE"나 "Electron"을 선택하면 조용히 아무 일도 안 하는 대신, 실행
자체를 시작하지 않고 지원 안 한다고 안내합니다.

### 실행 흐름

`orchestrator/plan_pipeline.py`의 `PlanDrivenPipeline`이 실제로 쓰이는
오케스트레이터입니다 (`orchestrator/pipeline.py`는 plan.json 기반 설계 이전의
구버전으로, 지금은 안 쓰이는 죽은 코드이고 참고용으로만 남아있습니다).
각 Phase에 대해 순서대로:

1. **Developer**가 지금까지 쌓인 코드 위에 이어서 구현합니다.
2. **Reviewer**가 그 Phase의 성공 조건 충족 여부를 판단합니다. 거부되면
   Developer가 다시 작성하고 Reviewer가 다시 검토합니다 (재시도 횟수 제한).
3. **GUI Tester**가 실제 앱을 조작하며 같은 조건을 시각적으로 확인합니다.
   실패하면 증상을 Developer에게 넘겨 재작성을 요청하고 다시 검증합니다
   (재시도 횟수 제한).
4. 재시도 예산을 다 쓰고도 실패하면 **Planner가 재계획**합니다 — 더 작은
   단위로 쪼개거나 다른 접근으로 — 같은 방식을 반복하지 말라는 지시와 함께.
5. 재계획으로도 해결 안 되면 **사람에게 물어봅니다** (재시도 / 방향 제시 /
   스킵 / 전체 중단).
6. 모든 Phase가 통과하면 개발 요약 리포트가 `logs/`에 Phase마다 누적
   저장됩니다 (중간에 죽어도 그때까지 완료된 기록은 남습니다).

### 실행 방법 두 가지

**채팅 대시보드 (권장)** — Streamlit UI에서 백그라운드로 파이프라인을 돌리고
진행 상황을 채팅 메시지로 보여주며, 사람 개입이 필요하면 채팅으로 물어봅니다:

```bash
uv run streamlit run dashboard.py
```

**일반 CLI** — `config/requirement.txt`의 요구사항을 읽어서 처음부터 끝까지
실행하고, 필요하면 콘솔로 물어봅니다:

```bash
uv run python main.py
```

어느 쪽이든 **GUI Tester가 검증하는 동안 실제 마우스/키보드를 가져다 씁니다**
— 실행 중에는 컴퓨터를 건드리지 마세요.

### 환경 설정

Python 3.12 기준입니다.

```bash
uv sync
cp .env.example .env   # LLM_PROVIDER 고르고, 그 provider의 API 키 입력
```

주요 `.env` 설정: `LLM_PROVIDER`(openai/anthropic/gemini/ollama, 4개
에이전트 전부 한 번에), `PLANNER_MODEL` / `DEVELOPER_MODEL` /
`REVIEWER_MODEL` / `GUI_TESTER_MODEL`(에이전트별 모델 지정),
`MAX_DEV_REVIEW_RETRIES`, `MAX_GUI_TEST_RETRIES`, `MAX_REPLAN_ATTEMPTS`,
`DEBUG_INJECT_BUG` / `DEBUG_INJECT_PHASE_ID`(아래 참고).

### 폴더 구조

```
.
├── agents/
│   ├── base.py            # 추상 클래스 (어떤 에이전트든 구현체 교체 자유)
│   ├── models.py           # 공용 데이터클래스: Phase, DevResult, ReviewResult, GUITestResult 등
│   ├── schemas.py           # 각 에이전트의 구조화 LLM 출력용 Pydantic 스키마
│   ├── planner.py             # create_plan / replan / request_human_escalation / 누적 리포트 / lessons.md
│   ├── developer.py             # 코드 생성 + 자체 린트 재시도 루프
│   ├── reviewer.py                # 의미적 리뷰 + 재작성 요청 루프
│   ├── gui_tester.py                # 실행 방식 디스패치 + 캡처/판단/실행/검증 루프
│   ├── report.py                     # 개발 리포트 Markdown 렌더링 (순수 포맷팅, LLM 호출 없음)
│   ├── llm_client.py                  # 4개 에이전트가 공유하는 멀티 프로바이더 구조화 출력 호출
│   └── gui/
│       ├── execution.py                 # OS 레벨 전용: 스크린샷, 클릭, 키 입력, 창 관리
│       ├── detection.py                  # Set-of-marks: 테두리/채움 컨테이너 탐지 + 라벨링
│       ├── judgment.py                    # 판단 프롬프트 구성, agents/llm_client.py 호출
│       └── server.py                       # target-app/용 로컬 정적 파일 서버
├── orchestrator/
│   ├── config.py            # .env 기반 PipelineConfig
│   ├── plan_pipeline.py       # PlanDrivenPipeline -- 실제 사용되는 오케스트레이터
│   ├── pipeline.py              # 구버전 in-memory Orchestrator 설계 -- 안 씀, 참고용
│   ├── logging_setup.py           # 콘솔(간결)+파일(상세) 로깅, UTF-8 안전
│   ├── encoding.py                  # stdout/stderr UTF-8 재설정 헬퍼
│   └── proc.py                        # UTF-8 안전 서브프로세스 래퍼
├── debug/bug_injector.py    # app.js를 의도적으로 훼손해 GUI실패->재작성 루프 데모
├── scripts/                 # 개별 에이전트/서브시스템 스모크 테스트
├── tools/eslint.config.js   # Developer 자체 린트용 flat config eslint
├── config/requirement.txt   # main.py가 읽는 요구사항
├── target-app/              # 생성된 앱 결과물 (index.html/style.css/app.js) -- 소스 아님, 매 실행마다 재생성
├── state/plan.json          # 모든 Phase 상태의 단일 소스 (한 번의 실행)
├── state/lessons.md         # 실행 간 장기 기억 (위 설명 참고) -- 한 번의 실행 상태가 아님
├── logs/                    # 실행 로그, 누적 Markdown 리포트, GUI Tester 스크린샷
├── dashboard.py              # Streamlit 채팅 UI 진입점
└── main.py                   # 일반 CLI 진입점
```

### 개별 에이전트 테스트

각 에이전트는 `scripts/`를 통해 단독으로 실행/검증할 수 있습니다:

```bash
uv run python scripts/smoke_test_planner.py       # 요구사항 -> state/plan.json
uv run python scripts/smoke_test_developer.py     # 첫 pending Phase -> target-app/
uv run python scripts/smoke_test_reviewer.py      # dev_done Phase 리뷰
uv run python scripts/smoke_test_gui_detection.py # 실제 스크린샷에 Set-of-marks 탐지
uv run python scripts/smoke_test_gui_judgment.py  # Vision 판단 1회 호출
uv run python scripts/smoke_test_gui_verify.py    # Phase 하나에 대한 전체 캡처/판단/실행 루프
uv run python scripts/smoke_test_plan_pipeline.py # 재계획+사람 에스컬레이션 경로 (DEBUG_INJECT_BUG=true 필요)
uv run python scripts/reset_local_storage.py      # 전체 실행 없이 target-app의 localStorage만 초기화
```

`PlanDrivenPipeline.run_phase()` / `run_phase_with_replanning()`을 직접
호출하는 스크립트는 반드시 `planner.start_report()` / `finalize_report()`와
`gui_tester.cleanup()`으로 감싸야 합니다 — 기존 스크립트들의 패턴을 참고하세요.

### 재계획/재작성 루프 데모

`debug/bug_injector.py`는 `.env`의 `DEBUG_INJECT_BUG` / `DEBUG_INJECT_PHASE_ID`로
켜면, 일치하는 Phase의 Reviewer를 건너뛰고 GUI 검증 직전에 Developer의
`app.js`를 일부러 훼손합니다. `DEBUG_INJECT_PHASE_ID`는 쉼표로 구분된 후보
id 목록을 받습니다 (예: `phase-1,phase-2`) — 추가 기능을 실제로 구현하는
Phase 번호가 실행마다 달라지기 때문입니다. 그러면 GUI Tester가 (연출된)
진짜 버그를 잡아내고 Developer↔GUI Tester 재작성 루프가 실제로 돕니다.
**재계획**까지
보고 싶으면 `.env`의 `MAX_GUI_TEST_RETRIES`를 일시적으로 `1`로 낮추세요 —
안 그러면 보통 첫 재작성에서 바로 고쳐져서 재시도 예산이 소진되기 전에
끝나버립니다.

### UTF-8 전역 규칙

이 저장소의 모든 모듈은 Windows 콘솔 인코딩이 한글/유니코드를 깨뜨릴 수
있다는 걸 전제로, 다음을 명시적으로 지킵니다:

- 모든 파일 입출력에 `encoding="utf-8"` 명시
- JSON 저장 시 `ensure_ascii=False`
- 콘솔 진입점에서는 `orchestrator.encoding.ensure_utf8_stdio()` 호출
  (오케스트레이터를 거치는 경로는 `setup_logging()` 내부에서 이미 호출함)
- 서브프로세스 호출은 `orchestrator.proc.run_utf8()` 사용

새 모듈을 추가할 때도 동일한 규칙을 적용하세요.

### 라이선스

MIT — [`LICENSE`](./LICENSE) 참고.

전체 설계 배경 문서:
[`멀티에이전트_자동개발시스템_기획서_v2.md`](./멀티에이전트_자동개발시스템_기획서_v2.md).
