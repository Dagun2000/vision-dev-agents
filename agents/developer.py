"""Developer agent implementation.

Scope of this step: read state/plan.json, (re)generate the three static
app files on top of whatever already exists in target-app/ (accumulating
each Phase's changes on the previous one's), self-lint app.js, retry with
lint feedback fed back into the model, and write the resulting Phase
status ("dev_done" / "lint_failed") back to plan.json.

Semantic code review and GUI verification are out of scope here -- that's
the Reviewer / GUI Tester agents' job in a later step.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from agents.base import DeveloperAgent
from agents.llm_client import TextPart, structured_completion
from agents.models import DevResult, LaunchConfig, LaunchType, Phase, PhaseStatus
from agents.schemas import DevOutputSchema
from orchestrator.config import ROOT_DIR, PipelineConfig
from orchestrator.proc import run_utf8

logger = logging.getLogger("pipeline")

MAX_JS_LINT_RETRIES = 3
ESLINT_CONFIG = ROOT_DIR / "tools" / "eslint.config.js"
# Always the renderer/UI files, for both launch types -- a static web app
# serves them directly, an Electron app's BrowserWindow loads the same
# index.html as its renderer.
APP_FILES = ("index.html", "style.css", "app.js")
# Electron's main-process entry point + manifest, additionally required
# only when target_launch_type is electron_app.
ELECTRON_FILES = ("main.js", "package.json")

SYSTEM_PROMPT = """\
당신은 멀티 에이전트 자동 개발 파이프라인의 개발자(Developer) 에이전트입니다.
목표는 localStorage만 사용하는 순수 정적 프론트엔드 Todo 리스트 앱을
index.html / style.css / app.js 세 파일로 점진적으로 완성하는 것입니다.

규칙:
- 서버, 빌드 도구, 외부 라이브러리(import/CDN 등) 없이 순수 HTML/CSS/
  바닐라 JavaScript만 사용하세요.
- 이번에 주어지는 "현재 파일 내용"은 이전 Phase까지 누적된 결과물입니다.
  이번 Phase의 요구사항만큼만 이어서 추가/수정하고, 기존에 이미 동작하던
  기능을 망가뜨리지 마세요.
- 응답에는 세 파일의 "전체" 내용을 다시 작성해서 반환하세요 (diff가 아닙니다).
  변경이 필요 없는 파일도 전체 내용을 그대로 반환하세요.
- app.js는 브라우저에 <script>로 그대로 로드되는 스크립트입니다. 전역
  window/document/localStorage/console만 사용할 수 있고, import/require,
  module 문법은 금지합니다.
- 이전 시도의 eslint 오류가 주어지면 그 오류를 반드시 고치세요.
- launch_config는 이 앱을 어떻게 실행해서 검증할지에 대한 정보입니다.
  launch_type은 항상 "static_web_server"로 고정하세요 (다른 값은 아직
  지원되지 않습니다). launch_command는 "python -m http.server 8000"과
  같은 형태로, entry_url은 "http://localhost:8000/index.html"과 같은
  형태로 작성하세요.
"""

ELECTRON_SYSTEM_PROMPT = """\
당신은 멀티 에이전트 자동 개발 파이프라인의 개발자(Developer) 에이전트입니다.
목표는 localStorage만 사용하는 Todo 리스트 앱을 Electron 데스크톱 앱으로,
index.html / style.css / app.js / main.js / package.json 다섯 파일로
점진적으로 완성하는 것입니다.

규칙:
- index.html/style.css/app.js는 Electron의 BrowserWindow가 그대로 불러오는
  렌더러 화면입니다. localStorage만 사용하는 순수 HTML/CSS/바닐라
  JavaScript로 작성하세요 (외부 라이브러리, CDN, import/require, module
  문법 금지 -- 정적 웹앱 버전과 동일한 제약입니다).
- 이번에 주어지는 "현재 파일 내용"은 이전 Phase까지 누적된 결과물입니다.
  이번 Phase의 요구사항만큼만 이어서 추가/수정하고, 기존에 이미 동작하던
  기능을 망가뜨리지 마세요.
- 응답에는 다섯 파일의 "전체" 내용을 다시 작성해서 반환하세요 (diff가
  아닙니다). 변경이 필요 없는 파일도 전체 내용을 그대로 반환하세요.
- main.js는 Electron 메인 프로세스 진입점입니다. `app.whenReady()` 이후
  `BrowserWindow`를 생성하고 `loadFile('index.html')`로 렌더러를
  불러오세요. nodeIntegration/contextIsolation 같은 보안 관련 옵션은
  기본값 그대로 두세요 (렌더러 쪽에서 Node API를 직접 쓰지 않습니다).
- package.json은 반드시 `"main": "main.js"`를 포함하세요. dependencies는
  비워두거나 생략하세요 (electron 자체는 실행 환경이 별도로 설치/관리합니다
  -- 여기에 적어도 실제로 설치되지 않습니다).
- 이전 시도의 eslint 오류가 주어지면 그 오류를 반드시 고치세요 (app.js만
  검사 대상입니다).
- launch_config.launch_type은 항상 "electron_app"으로 고정하세요.
  launch_command/entry_url은 실제 실행에는 쓰이지 않으니 대략적인 설명만
  적으면 됩니다.
"""


class DeveloperLintError(RuntimeError):
    """Raised when app.js still fails lint after MAX_JS_LINT_RETRIES attempts."""


class OpenAIDeveloperAgent(DeveloperAgent):
    """Named for historical reasons -- actually multi-provider via
    agents/llm_client.py and the single LLM_PROVIDER setting."""

    def __init__(self, config: PipelineConfig | None = None) -> None:
        self.config = config or PipelineConfig()

    # ---- DeveloperAgent interface (used by the orchestrator loop) -----

    def implement(
        self,
        phase: Phase,
        feedback: str | None = None,
        review_issues: list[str] | None = None,
    ) -> DevResult:
        """Implement (or revise) a Phase.

        `feedback` is a free-form string used by the orchestrator's generic
        retry loop (review/GUI issues joined into one message). `review_issues`
        is the same kind of input but structured, used when the Reviewer
        agent explicitly requests a rewrite -- kept separate from the
        internal lint-retry feedback below so the model sees "reviewer
        found these semantic problems" and "eslint found these syntax
        problems" as distinct, clearly-labelled sections.
        """
        current_files = self._read_current_files()
        lint_feedback = feedback
        lint_errors_seen: list[str] = []

        for attempt in range(1, MAX_JS_LINT_RETRIES + 1):
            logger.info(
                "Developer: phase=%s attempt=%d/%d (model=%s)",
                phase.id,
                attempt,
                MAX_JS_LINT_RETRIES,
                self.config.developer_model,
            )
            generated = self._generate_files(phase, current_files, lint_feedback, review_issues)
            self._write_files(generated)

            lint_ok, lint_message = self._lint_js(self.config.target_app_dir / "app.js")
            if lint_ok:
                logger.info("Developer: phase=%s lint passed -- %s", phase.id, lint_message)
                return DevResult(
                    phase_id=phase.id,
                    summary=generated.summary,
                    files_changed=list(self._current_file_names()),
                    launch_config=LaunchConfig(
                        launch_type=generated.launch_config.launch_type,
                        launch_command=generated.launch_config.launch_command,
                        entry_url=generated.launch_config.entry_url,
                    ),
                    lint_attempts=attempt,
                    lint_errors=lint_errors_seen,
                )

            lint_errors_seen.append(lint_message)
            logger.warning(
                "Developer: phase=%s lint failed (attempt %d/%d) -- %s",
                phase.id,
                attempt,
                MAX_JS_LINT_RETRIES,
                lint_message,
            )
            lint_feedback = lint_message
            current_files = {
                "index.html": generated.index_html,
                "style.css": generated.style_css,
                "app.js": generated.app_js,
            }

        raise DeveloperLintError(
            f"Phase {phase.id}: app.js failed lint after {MAX_JS_LINT_RETRIES} attempts"
        )

    # ---- plan.json-driven workflow (standalone / CLI entry point) -----

    def develop_next_pending_phase(self) -> DevResult:
        """Read state/plan.json, implement the first 'pending' Phase, and
        write the resulting status ('dev_done' / 'lint_failed') back."""
        plan_path = self.config.plan_file
        plan = json.loads(plan_path.read_text(encoding="utf-8"))

        phase_dict = next(
            (p for p in plan["phases"] if p["status"] == PhaseStatus.PENDING.value), None
        )
        if phase_dict is None:
            raise ValueError(f"{plan_path} has no phase with status='pending'")

        phase = Phase(
            id=phase_dict["id"],
            title=phase_dict["title"],
            description=phase_dict["description"],
            success_criteria=phase_dict["success_criteria"],
            status=PhaseStatus.PENDING,
        )

        try:
            dev_result = self.implement(phase)
        except DeveloperLintError:
            phase_dict["status"] = PhaseStatus.LINT_FAILED.value
            plan_path.write_text(
                json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            logger.error("Developer: phase=%s marked lint_failed in %s", phase.id, plan_path)
            raise

        phase_dict["status"] = PhaseStatus.DEV_DONE.value
        if dev_result.launch_config is not None:
            phase_dict["launch_config"] = {
                "launch_type": dev_result.launch_config.launch_type.value,
                "launch_command": dev_result.launch_config.launch_command,
                "entry_url": dev_result.launch_config.entry_url,
            }
        phase_dict["lint_attempts"] = dev_result.lint_attempts
        phase_dict["lint_errors"] = dev_result.lint_errors
        phase_dict["dev_summary"] = dev_result.summary
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Developer: phase=%s marked dev_done in %s", phase.id, plan_path)
        return dev_result

    # ---- internals ------------------------------------------------------

    def _current_file_names(self) -> tuple[str, ...]:
        if self.config.target_launch_type == LaunchType.ELECTRON_APP:
            return APP_FILES + ELECTRON_FILES
        return APP_FILES

    def _read_current_files(self) -> dict[str, str]:
        files: dict[str, str] = {}
        for name in self._current_file_names():
            path = self.config.target_app_dir / name
            files[name] = path.read_text(encoding="utf-8") if path.exists() else ""
        return files

    def _write_files(self, generated: DevOutputSchema) -> None:
        self.config.target_app_dir.mkdir(parents=True, exist_ok=True)
        (self.config.target_app_dir / "index.html").write_text(
            generated.index_html, encoding="utf-8"
        )
        (self.config.target_app_dir / "style.css").write_text(
            generated.style_css, encoding="utf-8"
        )
        (self.config.target_app_dir / "app.js").write_text(generated.app_js, encoding="utf-8")
        # Electron-only -- both null for static_web_server (see
        # DevOutputSchema), so this is a no-op for the existing web path.
        if generated.main_js is not None:
            (self.config.target_app_dir / "main.js").write_text(
                generated.main_js, encoding="utf-8"
            )
        if generated.package_json is not None:
            (self.config.target_app_dir / "package.json").write_text(
                generated.package_json, encoding="utf-8"
            )
            self._ensure_electron_installed()

    def _ensure_electron_installed(self) -> None:
        """electron itself is never trusted from the LLM's package.json --
        same "don't exec LLM output for anything execution-critical" rule
        as launch_command (see agents/gui_tester.py). Idempotent: skips
        reinstalling if the binary's already there, since this can run
        again on every lint/review retry within the same Phase.

        Two steps, confirmed necessary by direct testing -- this package
        version's package.json has an *empty* "scripts" object, so `npm
        install electron` alone only gets the small JS wrapper (fast, ~1s)
        and never downloads the actual electron.exe; the binary only gets
        fetched by explicitly running the wrapper's own install.js
        afterward (also fast once npm's cache is warm, but does a real
        download the first time -- hence the generous timeouts).
        """
        electron_bin = (
            self.config.target_app_dir / "node_modules" / "electron" / "dist" / "electron.exe"
        )
        if electron_bin.exists():
            return
        npm_path = shutil.which("npm")
        node_path = shutil.which("node")
        if npm_path is None or node_path is None:
            logger.warning("Developer: npm/node not found on PATH, cannot install electron")
            return
        logger.info("Developer: installing electron in %s", self.config.target_app_dir)
        try:
            run_utf8(
                [npm_path, "install", "electron", "--no-fund", "--no-audit"],
                cwd=str(self.config.target_app_dir),
                timeout=300,
            )
            install_script = self.config.target_app_dir / "node_modules" / "electron" / "install.js"
            if install_script.exists():
                run_utf8([node_path, str(install_script)], cwd=str(self.config.target_app_dir), timeout=300)
        except subprocess.TimeoutExpired:
            logger.warning("Developer: electron install timed out")
        if not electron_bin.exists():
            logger.warning(
                "Developer: electron.exe still missing after install attempt (%s)", electron_bin
            )

    def _generate_files(
        self,
        phase: Phase,
        current_files: dict[str, str],
        lint_feedback: str | None,
        review_issues: list[str] | None = None,
    ) -> DevOutputSchema:
        user_message = self._build_user_message(phase, current_files, lint_feedback, review_issues)
        system_prompt = (
            ELECTRON_SYSTEM_PROMPT
            if self.config.target_launch_type == LaunchType.ELECTRON_APP
            else SYSTEM_PROMPT
        )
        return structured_completion(
            config=self.config,
            model=self.config.developer_model,
            system_prompt=system_prompt,
            parts=[TextPart(user_message)],
            schema=DevOutputSchema,
        )

    def _build_user_message(
        self,
        phase: Phase,
        current_files: dict[str, str],
        lint_feedback: str | None,
        review_issues: list[str] | None = None,
    ) -> str:
        criteria = "\n".join(f"- {c}" for c in phase.success_criteria)
        parts = [
            f"## 이번 Phase\nid: {phase.id}\n제목: {phase.title}\n설명: {phase.description}",
            f"\n## 성공 조건\n{criteria}",
            "\n## 현재 파일 내용 (이전 Phase까지 누적된 결과물)",
        ]
        for name in self._current_file_names():
            content = current_files.get(name, "")
            if content:
                parts.append(f"\n### {name}\n```\n{content}\n```")
            else:
                parts.append(f"\n### {name}\n(아직 없음 -- 새로 작성)")
        if review_issues:
            issues_text = "\n".join(f"- {issue}" for issue in review_issues)
            parts.append(f"\n## 코드 리뷰어가 지적한 문제 (반드시 고칠 것)\n{issues_text}")
        if lint_feedback:
            parts.append(f"\n## 이전 시도의 lint 오류 (반드시 고칠 것)\n{lint_feedback}")
        return "\n".join(parts)

    def _lint_js(self, js_path: Path) -> tuple[bool, str]:
        if not js_path.exists():
            return False, f"{js_path} 파일이 생성되지 않았습니다"

        eslint_result = self._run_eslint(js_path)
        if eslint_result is not None:
            return eslint_result

        logger.warning("Developer: eslint unavailable, falling back to `node --check`")
        return self._run_node_check(js_path)

    def _run_eslint(self, js_path: Path) -> tuple[bool, str] | None:
        """Run eslint (flat config, core rules only) as the JS linter.

        Returns None (not a lint result) if eslint itself couldn't be run
        -- e.g. not installed/cached for npx -- so the caller can fall
        back to a plain syntax check instead.
        """
        # subprocess.run() without shell=True can't resolve Windows .cmd
        # shims (npx.CMD) by bare name -- resolve the real path via PATH.
        npx_path = shutil.which("npx")
        if npx_path is None:
            logger.warning("Developer: npx not found on PATH")
            return None

        try:
            result = run_utf8(
                [
                    npx_path,
                    "--no-install",
                    "eslint",
                    "--no-config-lookup",
                    "--config",
                    str(ESLINT_CONFIG),
                    "--format",
                    "json",
                    str(js_path),
                ],
                cwd=str(ROOT_DIR),
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            logger.warning("Developer: npx eslint invocation failed (%s)", exc)
            return None

        try:
            report = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "Developer: eslint did not return JSON (npx/eslint unavailable?), stderr=%s",
                result.stderr.strip(),
            )
            return None

        errors = [
            f"{js_path.name}:{message['line']}:{message['column']} "
            f"{message.get('ruleId') or 'error'}: {message['message']}"
            for file_report in report
            for message in file_report.get("messages", [])
            if message.get("severity") == 2
        ]
        if errors:
            return False, "\n".join(errors)
        return True, "eslint: no errors"

    def _run_node_check(self, js_path: Path) -> tuple[bool, str]:
        node_path = shutil.which("node")
        if node_path is None:
            return False, "node를 찾을 수 없어 문법 검사를 수행하지 못했습니다"

        try:
            result = run_utf8([node_path, "--check", str(js_path)], cwd=str(ROOT_DIR), timeout=30)
        except subprocess.TimeoutExpired:
            return False, "node --check 실행이 시간 초과되었습니다"

        if result.returncode == 0:
            return True, "node --check: no syntax errors (eslint unavailable, syntax-only check)"
        return False, (result.stderr.strip() or result.stdout.strip())
