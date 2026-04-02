"""
Local build checker for MR source branches.

Flow:
  1. Clone (or fetch) the source project into a persistent local cache
  2. Create a throwaway git worktree for the source branch
  3. Auto-detect tech stack from files in the repo root
  4. Check if the required tool is available locally — skip cleanly if not
  5. Run the appropriate compile/type-check command
  6. Classify the result:
       PASSED      - compilation/type-check succeeded
       CODE_ERROR  - real compilation/type errors in the code
       DEP_ERROR   - dependency/lib resolution failure → ignored, don't block
       SKIPPED     - tool not installed, gradlew missing, worktree failed, unknown stack
       ERROR       - unexpected failure during the check itself
"""

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_WORKSPACE       = Path(os.getenv('BUILD_WORKSPACE', '/tmp/mr-review-bot'))
_BUILD_TIMEOUT   = int(os.getenv('BUILD_TIMEOUT_SECONDS', 300))
_GIT_ENV         = {**os.environ, 'GIT_SSL_NO_VERIFY': '1', 'GIT_TERMINAL_PROMPT': '0'}


# ---------------------------------------------------------------------------
# Tech-stack detection
# ---------------------------------------------------------------------------
def detect_stack(worktree_dir: Path) -> Optional[Dict]:
    """
    Inspect the repo root and return the best build strategy, or None if unknown.

    Returns a dict:
        {
            "name":    human-readable name,
            "tool":    binary to check in PATH (or None),
            "fn":      function(worktree_dir) -> (returncode, output_str),
            "classify": function(output, rc) -> BuildResult
        }
    """
    has = lambda *names: any((worktree_dir / n).exists() for n in names)

    if has('gradlew', 'build.gradle', 'build.gradle.kts'):
        return {
            'name':     'Gradle (Java/Kotlin)',
            'tool':     'java',
            'fn':       _build_gradle,
            'classify': _classify_jvm,
        }

    if has('pom.xml'):
        return {
            'name':     'Maven (Java)',
            'tool':     'mvn',
            'fn':       _build_maven,
            'classify': _classify_jvm,
        }

    # TypeScript check takes priority over plain npm build
    if has('package.json') and has('tsconfig.json', 'tsconfig.app.json', 'tsconfig.base.json'):
        return {
            'name':     'TypeScript / React Native',
            'tool':     'node',
            'fn':       _build_typescript,
            'classify': _classify_typescript,
        }

    if has('package.json'):
        return {
            'name':     'Node.js / React Native',
            'tool':     'node',
            'fn':       _build_node,
            'classify': _classify_node,
        }

    if has('pyproject.toml', 'setup.py', 'requirements.txt') or list(worktree_dir.glob('*.py')):
        return {
            'name':     'Python',
            'tool':     'python3',
            'fn':       _build_python,
            'classify': _classify_python,
        }

    return None


def is_tool_available(tool: Optional[str]) -> bool:
    if not tool:
        return True
    return shutil.which(tool) is not None


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class BuildResult:
    status: str                          # PASSED | CODE_ERROR | DEP_ERROR | SKIPPED | ERROR
    stack:  str = 'unknown'              # detected stack name
    errors: List[str] = field(default_factory=list)
    raw_output: str = ''

    @property
    def has_code_errors(self) -> bool:
        return self.status == 'CODE_ERROR'

    @property
    def should_block(self) -> bool:
        return self.status == 'CODE_ERROR'

    def as_comment_section(self) -> str:
        if not self.has_code_errors:
            return ''
        lines = [
            f'### 🔨 Local Build Check — FAILED ({self.stack})',
            '',
            'Compilation errors found on the source branch:',
            '',
        ]
        for err in self.errors[:30]:
            lines.append(f'```\n{err}\n```')
        if len(self.errors) > 30:
            lines.append(f'_...and {len(self.errors) - 30} more (see full build log)_')
        lines += ['', '> ⛔ Merge blocked until compilation errors are resolved.']
        return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def check_mr_build(mr, gl_client) -> BuildResult:
    """
    Clone/fetch the MR source branch, detect the stack, run the build.
    Never raises — all exceptions are caught.
    """
    source_branch      = getattr(mr, 'source_branch', None)
    source_project_id  = getattr(mr, 'source_project_id', None) or mr.project_id

    if not source_branch:
        log.warning('Build check skipped: no source_branch on MR')
        return BuildResult(status='SKIPPED', raw_output='No source branch available.')

    try:
        project   = gl_client.gl.projects.get(source_project_id)
        clone_url = _inject_token(project.http_url_to_repo, os.getenv('GITLAB_TOKEN', ''))
    except Exception as e:
        log.error(f'Build check: could not get project info: {e}')
        return BuildResult(status='ERROR', raw_output=str(e))

    repo_dir     = _WORKSPACE / str(source_project_id)
    worktree_dir = _WORKSPACE / f'{source_project_id}-wt-{_safe_name(source_branch)}'

    try:
        _ensure_repo(repo_dir, clone_url)
        _ensure_worktree(repo_dir, worktree_dir, source_branch)
        return _run_build(worktree_dir, mr.iid)
    except Exception as e:
        log.error(f'Build check unexpected error for MR !{mr.iid}: {e}')
        return BuildResult(status='ERROR', raw_output=str(e))
    finally:
        _cleanup_worktree(repo_dir, worktree_dir)


# ---------------------------------------------------------------------------
# Build runners — one per stack
# ---------------------------------------------------------------------------
def _build_gradle(worktree_dir: Path) -> Tuple[int, str]:
    gradlew = worktree_dir / 'gradlew'
    if gradlew.exists():
        gradlew.chmod(0o755)
        cmd = ['./gradlew', 'compileJava', 'compileKotlin',
               '--no-daemon', '--continue', '-x', 'test', '-x', 'processResources']
        runner = str(worktree_dir)
    else:
        # Fall back to system gradle if gradlew is missing
        if not shutil.which('gradle'):
            return -1, 'gradlew not found and system gradle not installed.'
        cmd = ['gradle', 'compileJava', '--no-daemon', '-x', 'test']
        runner = str(worktree_dir)

    result = subprocess.run(
        cmd, cwd=runner, capture_output=True, text=True, timeout=_BUILD_TIMEOUT,
        env={**os.environ, 'GRADLE_OPTS': '-Xmx512m', 'GIT_SSL_NO_VERIFY': '1'},
    )
    return result.returncode, result.stdout + result.stderr


def _build_maven(worktree_dir: Path) -> Tuple[int, str]:
    result = subprocess.run(
        ['mvn', 'compile', '-q', '--no-transfer-progress', '-DskipTests'],
        cwd=worktree_dir, capture_output=True, text=True, timeout=_BUILD_TIMEOUT,
        env={**os.environ, 'GIT_SSL_NO_VERIFY': '1'},
    )
    return result.returncode, result.stdout + result.stderr


def _build_typescript(worktree_dir: Path) -> Tuple[int, str]:
    """npm install (offline if possible) then tsc --noEmit."""
    _npm_install(worktree_dir)
    result = subprocess.run(
        ['npx', '--yes', 'tsc', '--noEmit'],
        cwd=worktree_dir, capture_output=True, text=True, timeout=_BUILD_TIMEOUT,
        env={**os.environ, 'CI': '1'},
    )
    return result.returncode, result.stdout + result.stderr


def _build_node(worktree_dir: Path) -> Tuple[int, str]:
    """npm install then npm run build (if script exists), else npm run lint."""
    _npm_install(worktree_dir)

    # Read package.json to check available scripts
    pkg_json = worktree_dir / 'package.json'
    scripts = {}
    try:
        scripts = json.loads(pkg_json.read_text()).get('scripts', {})
    except Exception:
        pass

    script = 'build' if 'build' in scripts else ('lint' if 'lint' in scripts else None)
    if not script:
        # Nothing useful to run, just install was the check
        return 0, 'npm install succeeded. No build/lint script found — skipping compile check.'

    result = subprocess.run(
        ['npm', 'run', script],
        cwd=worktree_dir, capture_output=True, text=True, timeout=_BUILD_TIMEOUT,
        env={**os.environ, 'CI': '1'},
    )
    return result.returncode, result.stdout + result.stderr


def _build_python(worktree_dir: Path) -> Tuple[int, str]:
    """Byte-compile all Python files to catch syntax errors."""
    result = subprocess.run(
        ['python3', '-m', 'compileall', '-q', '.'],
        cwd=worktree_dir, capture_output=True, text=True, timeout=_BUILD_TIMEOUT,
    )
    return result.returncode, result.stdout + result.stderr


def _npm_install(worktree_dir: Path):
    """Run npm install quietly (prefer offline cache to avoid network issues)."""
    pkg_lock = worktree_dir / 'package-lock.json'
    yarn_lock = worktree_dir / 'yarn.lock'
    try:
        if yarn_lock.exists():
            subprocess.run(
                ['yarn', 'install', '--frozen-lockfile', '--prefer-offline', '--silent'],
                cwd=worktree_dir, capture_output=True, timeout=180,
                env={**os.environ, 'CI': '1'},
            )
        else:
            ci_flag = ['ci'] if pkg_lock.exists() else ['install']
            subprocess.run(
                ['npm', *ci_flag, '--prefer-offline', '--silent'],
                cwd=worktree_dir, capture_output=True, timeout=180,
                env={**os.environ, 'CI': '1'},
            )
    except Exception as e:
        log.warning(f'npm/yarn install warning: {e}')


# ---------------------------------------------------------------------------
# Error classifiers — one per stack family
# ---------------------------------------------------------------------------

# ── JVM (Gradle + Maven) ──────────────────────────────────────────────────

_JVM_DEP_RE = re.compile(
    r'Could not resolve|Could not download|Could not find|Could not GET|Could not HEAD|'
    r'Unable to load Maven meta-data|No cached version|Connection refused|Connection timed out|'
    r'connect timed out|PKIX path building|sun\.security\.validator|SSLHandshakeException|'
    r'Received status code [45]\d\d|Failed to transfer|Could not transfer artifact|'
    r'UnknownHostException|Name or service not known|'
    r'ot-commons-core|ot-cache-manager|ot-commons-adapter|com\.omantel',
    re.IGNORECASE
)
_JVM_CODE_RE = re.compile(
    r'[\w/\-\.]+\.java:\d+:\s*error:|'
    r'[\w/\-\.]+\.kt:\d+:\d+:\s*error:|'
    r"error: ';' expected|error: reached end of file|error: illegal start|"
    r'error: not a statement|error: unclosed string|error: class, interface|'
    r'error: incompatible types|error: method .+ is not applicable|'
    r'error: variable .+ might not have been initialized|error: duplicate class|'
    r'error: .+ has private access|error: .+ is already defined',
    re.IGNORECASE
)
_COMPILE_TASK_RE = re.compile(
    r"Execution failed for task ':(compileJava|compileKotlin|compileTestJava|compile)'",
    re.IGNORECASE
)
_MAVEN_COMPILE_FAIL_RE = re.compile(r'BUILD FAILURE', re.IGNORECASE)


def _classify_jvm(output: str, rc: int) -> 'BuildResult':
    if rc == 0:
        return BuildResult(status='PASSED')
    lines = output.splitlines()
    dep_lines  = [l for l in lines if _JVM_DEP_RE.search(l)]
    code_lines = [l for l in lines if _JVM_CODE_RE.search(l)]
    compile_failed = bool(_COMPILE_TASK_RE.search(output) or _MAVEN_COMPILE_FAIL_RE.search(output))

    if code_lines and compile_failed:
        errors = _extract_jvm_errors(lines)
        return BuildResult(status='CODE_ERROR', errors=errors, raw_output=output)
    if dep_lines:
        return BuildResult(status='DEP_ERROR', raw_output=output)
    return BuildResult(status='SKIPPED', raw_output=output[:3000])


def _extract_jvm_errors(lines: List[str]) -> List[str]:
    errors, i = [], 0
    while i < len(lines):
        if _JVM_CODE_RE.search(lines[i]) and not _JVM_DEP_RE.search(lines[i]):
            block = lines[i: i + 4]
            errors.append('\n'.join(block))
            i += 4
        else:
            i += 1
    return errors


# ── TypeScript ───────────────────────────────────────────────────────────

_TS_CODE_RE = re.compile(
    r'[\w/\-\.]+\.tsx?(?:\(\d+,\d+\)|:\d+:\d+):\s*error\s+TS\d+:|'
    r'error TS\d+:',
    re.IGNORECASE
)
_TS_DEP_RE  = re.compile(
    r'Cannot find module|Could not resolve|npm ERR! 404|ENOTFOUND|ECONNREFUSED|'
    r'registry\.npmjs\.org|Network request failed',
    re.IGNORECASE
)


def _classify_typescript(output: str, rc: int) -> 'BuildResult':
    if rc == 0:
        return BuildResult(status='PASSED')
    lines = output.splitlines()
    code_lines = [l for l in lines if _TS_CODE_RE.search(l)]
    dep_lines  = [l for l in lines if _TS_DEP_RE.search(l)]
    if code_lines:
        errors = [l for l in code_lines[:30] if not _TS_DEP_RE.search(l)]
        if errors:
            return BuildResult(status='CODE_ERROR', errors=errors, raw_output=output)
    if dep_lines:
        return BuildResult(status='DEP_ERROR', raw_output=output)
    return BuildResult(status='SKIPPED', raw_output=output[:3000])


# ── Node ─────────────────────────────────────────────────────────────────

_NODE_CODE_RE = re.compile(
    r'SyntaxError:|TypeError:|ReferenceError:|'
    r'Module not found: Error:|Cannot find module \'[^\']+\' from (?!node_modules)',
    re.IGNORECASE
)
_NODE_DEP_RE = re.compile(
    r'npm ERR! 404|ENOTFOUND|ECONNREFUSED|registry\.npmjs|'
    r'Cannot find module \'[^\']+\' from node_modules|'
    r'Module not found.*node_modules',
    re.IGNORECASE
)


def _classify_node(output: str, rc: int) -> 'BuildResult':
    if rc == 0:
        return BuildResult(status='PASSED')
    lines = output.splitlines()
    code_lines = [l for l in lines if _NODE_CODE_RE.search(l) and not _NODE_DEP_RE.search(l)]
    dep_lines  = [l for l in lines if _NODE_DEP_RE.search(l)]
    if code_lines:
        return BuildResult(status='CODE_ERROR', errors=code_lines[:20], raw_output=output)
    if dep_lines:
        return BuildResult(status='DEP_ERROR', raw_output=output)
    return BuildResult(status='SKIPPED', raw_output=output[:3000])


# ── Python ───────────────────────────────────────────────────────────────

_PY_CODE_RE = re.compile(r'SyntaxError:|IndentationError:|TabError:', re.IGNORECASE)


def _classify_python(output: str, rc: int) -> 'BuildResult':
    if rc == 0:
        return BuildResult(status='PASSED')
    lines = output.splitlines()
    code_lines = [l for l in lines if _PY_CODE_RE.search(l)]
    if code_lines:
        return BuildResult(status='CODE_ERROR', errors=code_lines[:20], raw_output=output)
    return BuildResult(status='SKIPPED', raw_output=output[:3000])


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------
def _run_build(worktree_dir: Path, mr_iid: int) -> BuildResult:
    stack_info = detect_stack(worktree_dir)

    if not stack_info:
        log.info(f'MR !{mr_iid}: no recognisable build system found — skipping build check.')
        return BuildResult(status='SKIPPED', raw_output='No recognised build system (gradle/maven/npm/python).')

    stack_name = stack_info['name']
    tool       = stack_info.get('tool')

    if not is_tool_available(tool):
        log.warning(f'MR !{mr_iid}: {stack_name} detected but `{tool}` not installed locally — skipping.')
        return BuildResult(
            status='SKIPPED', stack=stack_name,
            raw_output=f'`{tool}` is not installed on this machine. Install it to enable {stack_name} build checks.',
        )

    log.info(f'MR !{mr_iid}: detected {stack_name} — running build check (timeout {_BUILD_TIMEOUT}s)...')

    try:
        rc, output = stack_info['fn'](worktree_dir)
    except subprocess.TimeoutExpired:
        log.warning(f'MR !{mr_iid}: {stack_name} build timed out after {_BUILD_TIMEOUT}s')
        return BuildResult(status='SKIPPED', stack=stack_name,
                           raw_output=f'Build timed out after {_BUILD_TIMEOUT}s.')
    except Exception as e:
        log.error(f'MR !{mr_iid}: {stack_name} build runner error: {e}')
        return BuildResult(status='ERROR', stack=stack_name, raw_output=str(e))

    result = stack_info['classify'](output, rc)
    result.stack = stack_name

    log.info(f'MR !{mr_iid}: build {result.status} ({stack_name})')
    return result


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------
def _run_git(args: list, cwd: Optional[Path] = None, timeout: int = 120) -> Tuple[int, str]:
    result = subprocess.run(
        ['git'] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=_GIT_ENV,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _ensure_repo(repo_dir: Path, clone_url: str):
    _WORKSPACE.mkdir(parents=True, exist_ok=True)
    if (repo_dir / '.git').exists():
        log.info(f'Fetching repo at {repo_dir}...')
        rc, out = _run_git(['fetch', '--all', '--prune'], cwd=repo_dir, timeout=120)
        if rc != 0:
            log.warning(f'git fetch warning: {out[:300]}')
    else:
        log.info(f'Cloning repo into {repo_dir}...')
        rc, out = _run_git(['clone', '--filter=blob:none', clone_url, str(repo_dir)], timeout=300)
        if rc != 0:
            raise RuntimeError(f'git clone failed: {out[:500]}')


def _ensure_worktree(repo_dir: Path, worktree_dir: Path, branch: str):
    if worktree_dir.exists():
        shutil.rmtree(worktree_dir, ignore_errors=True)
        _run_git(['worktree', 'prune'], cwd=repo_dir)
    log.info(f"Creating worktree for branch {branch!r} at {worktree_dir}")
    rc, out = _run_git(
        ['worktree', 'add', '--detach', str(worktree_dir), f'origin/{branch}'],
        cwd=repo_dir, timeout=60,
    )
    if rc != 0:
        raise RuntimeError(f'git worktree add failed for {branch!r}: {out[:500]}')


def _cleanup_worktree(repo_dir: Path, worktree_dir: Path):
    try:
        if worktree_dir.exists():
            shutil.rmtree(worktree_dir, ignore_errors=True)
        if repo_dir.exists():
            _run_git(['worktree', 'prune'], cwd=repo_dir)
    except Exception as e:
        log.warning(f'Worktree cleanup warning: {e}')


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def _inject_token(http_url: str, token: str) -> str:
    parsed = urlparse(http_url)
    gitlab_url = os.getenv('GITLAB_URL', '').rstrip('/')
    if gitlab_url:
        configured = urlparse(gitlab_url)
        parsed = parsed._replace(
            scheme=configured.scheme,
            netloc=f'oauth2:{token}@{configured.hostname}:{configured.port or 443}',
        )
    else:
        parsed = parsed._replace(netloc=f'oauth2:{token}@{parsed.hostname}')
    return urlunparse(parsed)


def _safe_name(branch: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_\-]', '_', branch)[:60]
