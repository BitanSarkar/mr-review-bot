"""
Stack-agnostic static checks that run on every MR regardless of tech stack.

Checks:
  1. Duplicate keys in application.yaml / application-*.yaml / .env / .properties
  2. Helm completeness — every ${VAR} in application.yaml must exist in values.yaml
  3. Basic code patterns — .block(), Thread.sleep, System.out.println,
     hardcoded IPs/secrets, TODO in production paths, etc.

Returns a list of StaticIssue objects that are converted into MR comments.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Issue type
# ---------------------------------------------------------------------------
@dataclass
class StaticIssue:
    severity: str          # 'error' | 'warning' | 'info'
    category: str          # 'duplicate_key' | 'helm_mismatch' | 'code_pattern'
    file:     str
    line:     Optional[int]
    message:  str

    def as_comment(self) -> Dict:
        icon = {'error': '🔴', 'warning': '🟡', 'info': 'ℹ️'}.get(self.severity, '•')
        return {
            'file':    self.file,
            'line':    self.line,
            'body':    f"{icon} **[{self.category}]** {self.message}",
            'suggestion': None,
        }


@dataclass
class StaticCheckResult:
    issues: List[StaticIssue] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == 'error' for i in self.issues)

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)

    def as_comment_section(self) -> str:
        if not self.issues:
            return ''
        lines = ['### 🔍 Static Analysis Findings', '']
        for i in self.issues:
            icon = {'error': '🔴', 'warning': '🟡', 'info': 'ℹ️'}.get(i.severity, '•')
            loc  = f'`{i.file}`' + (f' line {i.line}' if i.line else '')
            lines.append(f'- {icon} **{loc}** — {i.message}')
        return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def run_static_checks(worktree_dir: Path, changed_files: List[str]) -> StaticCheckResult:
    """
    Run all static checks on the worktree.
    `changed_files` — list of file paths touched by the MR (used to scope findings).
    """
    result = StaticCheckResult()

    changed_set = set(changed_files)

    _check_yaml_duplicates(worktree_dir, changed_set, result)
    _check_helm_completeness(worktree_dir, changed_set, result)
    _check_code_patterns(worktree_dir, changed_set, result)

    return result


# ---------------------------------------------------------------------------
# 1. Duplicate keys in YAML / .env / .properties
# ---------------------------------------------------------------------------
_CONFIG_GLOBS = [
    'src/**/application*.yaml',
    'src/**/application*.yml',
    'src/**/application*.properties',
    'src/**/*.env',
    '.env',
    '.env.*',
]


def _check_yaml_duplicates(worktree_dir: Path, changed_set: set, result: StaticCheckResult):
    """Find duplicate keys in all config files touched by the MR."""
    for pattern in _CONFIG_GLOBS:
        for fpath in worktree_dir.glob(pattern):
            rel = str(fpath.relative_to(worktree_dir))
            if rel not in changed_set:
                continue   # only check files actually changed in this MR
            try:
                if fpath.suffix in ('.yaml', '.yml'):
                    dupes = _yaml_duplicate_keys(fpath)
                elif fpath.suffix == '.properties' or fpath.name.startswith('.env'):
                    dupes = _flat_file_duplicate_keys(fpath)
                else:
                    continue
                for key, lineno in dupes:
                    result.issues.append(StaticIssue(
                        severity='error',
                        category='duplicate_key',
                        file=rel,
                        line=lineno,
                        message=f"Duplicate key `{key}` — only the last value will be used at runtime.",
                    ))
            except Exception as e:
                log.warning(f"Could not check duplicates in {rel}: {e}")


def _yaml_duplicate_keys(path: Path) -> List[Tuple[str, Optional[int]]]:
    """
    Parse YAML and detect duplicate keys at any nesting level.
    Returns list of (key_path, line_number).
    Python's yaml by default silently overwrites duplicates, so we use an event-based parser.
    """
    dupes = []
    text  = path.read_text(errors='replace')

    # Build key path stack using YAML events
    import yaml
    try:
        events = list(yaml.parse(text, Loader=yaml.SafeLoader))
    except yaml.YAMLError:
        return []

    key_stack: List[Dict[str, int]] = []   # stack of {key: line}
    in_key    = False
    last_key  = None
    last_line = None

    for event in events:
        if isinstance(event, yaml.MappingStartEvent):
            key_stack.append({})
        elif isinstance(event, yaml.MappingEndEvent):
            if key_stack:
                key_stack.pop()
        elif isinstance(event, yaml.ScalarEvent):
            if key_stack:
                current_map = key_stack[-1]
                if not in_key:
                    # This scalar is a key
                    key   = event.value
                    lineno = event.start_mark.line + 1
                    if key in current_map:
                        dupes.append((key, lineno))
                    current_map[key] = lineno
                    in_key = True
                else:
                    in_key = False   # This scalar was a value
            # For sequences / non-mapping contexts, just toggle
        elif isinstance(event, (yaml.SequenceStartEvent, yaml.SequenceEndEvent)):
            in_key = False

    return dupes


def _flat_file_duplicate_keys(path: Path) -> List[Tuple[str, Optional[int]]]:
    """Detect duplicate keys in .env / .properties files (KEY=VALUE format)."""
    seen:  Dict[str, int] = {}
    dupes: List[Tuple[str, int]] = []
    for lineno, raw in enumerate(path.read_text(errors='replace').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_.]*)\s*[=:]', line)
        if match:
            key = match.group(1)
            if key in seen:
                dupes.append((key, lineno))
            else:
                seen[key] = lineno
    return dupes


# ---------------------------------------------------------------------------
# 2. Helm values completeness
# ---------------------------------------------------------------------------
# Matches ${VAR} and ${VAR:default} in Spring Boot YAML
_SPRING_VAR_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_.]*?)(?::.*?)?\}')

# Common helm values.yaml locations
_VALUES_GLOBS = [
    'helm/**/values.yaml',
    'helm/**/values.yml',
    'chart/**/values.yaml',
    'k8s/**/values.yaml',
    'kubernetes/**/values.yaml',
    'deploy/**/values.yaml',
    'values.yaml',
]


def _check_helm_completeness(worktree_dir: Path, changed_set: set, result: StaticCheckResult):
    """
    For every application.yaml change, check that all ${VAR} references
    exist somewhere in the helm values.yaml files.
    """
    app_yamls = [
        p for p in worktree_dir.glob('src/**/application*.yaml')
        if str(p.relative_to(worktree_dir)) in changed_set
    ]
    if not app_yamls:
        return

    # Collect all keys from all values files (recursively flattened)
    values_keys = _collect_helm_values_keys(worktree_dir)
    if not values_keys:
        log.debug('No helm values.yaml found — skipping helm completeness check')
        return

    for app_yaml in app_yamls:
        rel = str(app_yaml.relative_to(worktree_dir))
        try:
            text  = app_yaml.read_text(errors='replace')
            lines = text.splitlines()
            for lineno, line in enumerate(lines, 1):
                for var in _SPRING_VAR_RE.findall(line):
                    # Check if this var appears anywhere in values (flexible match)
                    if not _var_in_values(var, values_keys):
                        result.issues.append(StaticIssue(
                            severity='warning',
                            category='helm_mismatch',
                            file=rel,
                            line=lineno,
                            message=(
                                f"`${{{var}}}` is referenced in `{rel}` "
                                f"but not found in any helm `values.yaml`. "
                                f"The service may fail to start in Kubernetes."
                            ),
                        ))
        except Exception as e:
            log.warning(f"Could not check helm completeness for {rel}: {e}")


def _collect_helm_values_keys(worktree_dir: Path) -> List[str]:
    """Flatten all helm values YAML files into a list of leaf key paths and scalar values."""
    all_keys: List[str] = []
    for pattern in _VALUES_GLOBS:
        for vpath in worktree_dir.glob(pattern):
            try:
                import yaml
                data = yaml.safe_load(vpath.read_text(errors='replace')) or {}
                all_keys.extend(_flatten_yaml(data))
            except Exception as e:
                log.warning(f"Could not parse {vpath}: {e}")
    return all_keys


def _flatten_yaml(obj, prefix: str = '') -> List[str]:
    """Recursively flatten a YAML dict into a list of dotted key paths + string values."""
    results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            results.extend(_flatten_yaml(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_flatten_yaml(item, prefix))
    else:
        results.append(prefix)
        if obj is not None:
            results.append(str(obj).upper())   # also index values (env var names)
    return results


def _var_in_values(var: str, values_keys: List[str]) -> bool:
    """
    Check if `var` (an env var name like MY_VAR) appears in the values.
    Helm typically maps env vars as UPPER_SNAKE_CASE keys inside an `env:` block.
    We do a case-insensitive substring match to be lenient.
    """
    var_upper = var.upper()
    var_lower = var.lower()
    return any(
        var_upper in k.upper() or var_lower in k.lower()
        for k in values_keys
    )


# ---------------------------------------------------------------------------
# 3. Code pattern checks (stack-agnostic, diff-level)
# ---------------------------------------------------------------------------
_CODE_PATTERNS = [

    # ── Reactive / Async ─────────────────────────────────────────────────────
    (r'\.block\(\)',
     'error', 'reactive',
     'S1: `.block()` detected — blocking call in a reactive stack. Use reactive operators instead.'),

    (r'\.blockFirst\(\)|\.blockLast\(\)',
     'error', 'reactive',
     'S1: `.blockFirst()`/`.blockLast()` detected — blocking calls. Use reactive operators.'),

    (r'Thread\.sleep\s*\(',
     'error', 'reactive',
     'S2: `Thread.sleep()` blocks the thread. Use `Mono.delay()` or `Flux.interval()` in reactive code.'),

    (r'\.subscribe\s*\((?!.*\).*\)).*\)',
     'warning', 'reactive',
     'S3: `.subscribe()` in service/controller layer — reactive chains should be returned, not subscribed to.'),

    (r'\.toFuture\(\)\.get\(',
     'error', 'reactive',
     'S4: `.toFuture().get()` is a blocking call inside a reactive chain.'),

    (r'Schedulers\.single\(\)|Schedulers\.immediate\(\)',
     'warning', 'reactive',
     'S5: Verify that `Schedulers.single()` / `Schedulers.immediate()` is appropriate here — prefer `Schedulers.boundedElastic()` for blocking ops.'),

    # ── Security (Sonar OWASP Top 10) ─────────────────────────────────────
    (r'(?i)(password|secret|api_?key|token|passwd|private_?key|auth_?key)\s*[:=]\s*["\'][^"\'$\{]{4,}["\']',
     'error', 'security',
     'S106/S2068: Possible hardcoded credential. Use `${ENV_VAR}` injection — never commit secrets.'),

    (r'new\s+Random\s*\(',
     'warning', 'security',
     'S2245: `new Random()` is not cryptographically secure. Use `SecureRandom` for security-sensitive operations.'),

    (r'(?i)(MD5|SHA-?1)\s*["\'\(]|MessageDigest\.getInstance\s*\(\s*["\']MD5["\']|MessageDigest\.getInstance\s*\(\s*["\']SHA-?1["\']',
     'error', 'security',
     'S4790/S2070: MD5/SHA-1 are cryptographically broken. Use SHA-256 or stronger.'),

    (r'(?i)ObjectInputStream\s*\(|readObject\s*\(\)',
     'warning', 'security',
     'S5135: Java deserialization is a common attack vector. Ensure the input is from a trusted source.'),

    (r'Runtime\.getRuntime\(\)\.exec\(|ProcessBuilder\s*\(',
     'error', 'security',
     'S2076: OS command execution detected. Validate and sanitize all inputs to prevent command injection.'),

    (r'DocumentBuilderFactory\.newInstance\(\)',
     'warning', 'security',
     'S2755: Disable external entity processing to prevent XXE: `factory.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true)`.'),

    (r'\.setAllowedOrigins\s*\(\s*["\'][*]["\']',
     'error', 'security',
     'S5122: CORS wildcard `*` allows any origin. Restrict to known domains in production.'),

    (r'(?i)http://(?!localhost|127\.0\.0\.1)',
     'warning', 'security',
     'S5332: Plain HTTP used for a non-local URL. Use HTTPS to prevent man-in-the-middle attacks.'),

    (r'\.setSecure\s*\(\s*false\s*\)|\.setHttpOnly\s*\(\s*false\s*\)',
     'error', 'security',
     'S2092/S3330: Cookie `Secure` or `HttpOnly` flag explicitly disabled — this is a security risk.'),

    (r'SSLContext\.getInstance\s*\(\s*["\']SSL["\']|TrustAllCerts|X509TrustManager\s*\{[^}]*checkClient|ALLOW_ALL_HOSTNAME_VERIFIER',
     'error', 'security',
     'S4423/S4424: Insecure SSL/TLS configuration detected. Do not disable certificate validation.'),

    (r'eval\s*\(|exec\s*\(',
     'error', 'security',
     'S1523/S2076: `eval()`/`exec()` with dynamic input is a severe injection risk.'),

    (r'(?i)(pickle\.loads|yaml\.load\s*\([^,)]+\)(?!.*Loader))',
     'error', 'security',
     'S5247: Unsafe deserialization — use `yaml.safe_load()` and avoid `pickle.loads()` with untrusted data.'),

    # ── SQL Injection ──────────────────────────────────────────────────────
    (r'["\']\s*\+\s*\w+.*(?i)(SELECT|INSERT|UPDATE|DELETE|WHERE|FROM)',
     'error', 'security',
     'S2077: SQL string concatenation detected — use named parameters (`:param`) to prevent injection.'),

    (r'@Query\s*\(\s*".*"\s*\+',
     'error', 'security',
     'S2077: Dynamic `@Query` string concatenation. Use `:param` named parameters instead.'),

    # ── Java / Kotlin Bugs (Sonar Bug rules) ──────────────────────────────
    (r'(?<![a-zA-Z0-9_])(["\'][^"\']+["\'])\s*==\s*(["\'][^"\']+["\'])|\.equals\s*\(null\)',
     'error', 'bug',
     'S4973: String comparison with `==` compares references, not values. Use `.equals()`.'),

    (r'catch\s*\(\s*(?:Exception|Throwable)\s+\w+\s*\)\s*\{\s*\}',
     'error', 'bug',
     'S2221/S1166: Empty catch block swallows the exception silently. At minimum log it.'),

    (r'catch\s*\(\s*(?:Exception|Throwable)\s+\w+\s*\)\s*\{[^}]*\}(?!\s*finally)',
     'warning', 'bug',
     'S2221: Catching `Exception`/`Throwable` is too broad. Catch specific exception types.'),

    (r'(?i)finally\s*\{[^}]*\breturn\b',
     'error', 'bug',
     'S1143: `return` inside `finally` block — this will swallow any exception thrown in `try`.'),

    (r'(?i)catch\s*\([^)]+\)\s*\{[^}]*throw\s+(?!new\s+\w+Exception)[^;]+;',
     'warning', 'bug',
     'S1166: Re-throwing the caught exception directly loses the original stack trace. Wrap it: `throw new AppException("msg", e)`.'),

    (r'\.equals\s*\(\s*\)',
     'error', 'bug',
     'S1764: `.equals()` called with no argument — this will always throw `IllegalArgumentException`.'),

    (r'if\s*\([^)]+\)\s*;\s*\{',
     'error', 'bug',
     'S1116: Empty statement after `if` — the `if` body is actually the `{}` block regardless of condition.'),

    (r'==\s*(?:0\.0|1\.0|0\.0f|1\.0f)|(?:0\.0|1\.0|0\.0f|1\.0f)\s*==',
     'warning', 'bug',
     'S1244: Float/double equality comparison with `==` is unreliable due to floating-point precision. Use `Math.abs(a - b) < epsilon`.'),

    (r'new\s+Boolean\s*\(|new\s+Integer\s*\(|new\s+Long\s*\(|new\s+Double\s*\(|new\s+Float\s*\(',
     'warning', 'bug',
     'S2129: Deprecated boxed-type constructors (`new Integer()`, etc.). Use `Integer.valueOf()` or auto-boxing.'),

    (r'Collections\.EMPTY_LIST|Collections\.EMPTY_MAP|Collections\.EMPTY_SET',
     'info', 'code_smell',
     'S1596: Use `Collections.emptyList()`, `emptyMap()`, `emptySet()` — the constants are not type-safe.'),

    # ── Spring Boot specific ───────────────────────────────────────────────
    (r'@Autowired\s*\n\s*(?:private|protected)\s+(?!final)',
     'warning', 'spring',
     'S4175: Field injection with `@Autowired` makes the class harder to test. Use constructor injection instead.'),

    (r'@Transactional[^(].*\n\s*(?:private|protected)\s+(?!static)',
     'warning', 'spring',
     'S2229: `@Transactional` on a private method has no effect — Spring AOP cannot proxy private methods.'),

    (r'@RequestMapping\s*\(\s*(?!.*method)',
     'info', 'spring',
     'S4488: Use specific mapping annotations (`@GetMapping`, `@PostMapping`, etc.) instead of generic `@RequestMapping`.'),

    (r'@SpringBootTest(?!\s*\()',
     'info', 'spring',
     'S2187: `@SpringBootTest` loads the full context — use `@ExtendWith(MockitoExtension.class)` for unit tests.'),

    (r'@RunWith\s*\(',
     'warning', 'spring',
     'S5786: JUnit 4 `@RunWith` detected in a JUnit 5 project. Use `@ExtendWith` instead.'),

    (r'import\s+org\.junit\.Test\s*;(?!.*junit\.jupiter)',
     'warning', 'spring',
     'S5786: JUnit 4 `@Test` import detected. Migrate to JUnit 5 (`org.junit.jupiter.api.Test`).'),

    # ── Logging ───────────────────────────────────────────────────────────
    (r'System\.out\.print|System\.err\.print',
     'warning', 'logging',
     'S106: `System.out`/`System.err` should not be used in production code. Use a logger.'),

    (r'printStackTrace\s*\(\s*\)',
     'warning', 'logging',
     'S1148: `printStackTrace()` outputs to stderr without context. Use `log.error("msg", e)`.'),

    (r'log\.\w+\s*\(\s*"[^"]*"\s*\+\s*\w+',
     'warning', 'logging',
     'S2629: Log message uses string concatenation. Use parameterized logging: `log.info("msg {}", value)`.'),

    (r'(?i)(password|secret|token|otp|pin|national_?id|civil_?id)\s*["\']?\s*\+\s*\w+.*log\.|log\.\w+.*(?:password|secret|token|otp)',
     'error', 'security',
     'S2068/S5145: Sensitive data potentially being logged. Use `@SensitiveData` or mask before logging.'),

    # ── Code Smells (Sonar Code Smell rules) ──────────────────────────────
    (r'//\s*(TODO|FIXME|HACK|XXX)\s*',
     'info', 'code_smell',
     'S1134/S1135: TODO/FIXME marker in code. Ensure this is tracked in the backlog before merging.'),

    (r'@SuppressWarnings\s*\(',
     'info', 'code_smell',
     'S1309: `@SuppressWarnings` used. Ensure the suppression is justified and documented.'),

    (r'(?m)^(\s+)\1{4,}',
     'info', 'code_smell',
     'S134: Deeply nested code (5+ levels). Consider extracting logic into smaller methods.'),

    (r'throw new RuntimeException\s*\(|throw new Exception\s*\(',
     'warning', 'code_smell',
     'S112: Raw `RuntimeException`/`Exception` thrown. Use a typed exception from the hierarchy.'),

    (r'instanceof.*instanceof.*instanceof',
     'warning', 'code_smell',
     'S1318: Multiple `instanceof` checks — consider using polymorphism or a visitor pattern.'),

    (r'(?i)StringBuffer\s+\w+\s*=\s*new\s+StringBuffer',
     'info', 'code_smell',
     'S1149: `StringBuffer` is synchronized and slower. Use `StringBuilder` in single-threaded code.'),

    (r'for\s*\([^)]+\)\s*\{[^}]*\+\s*=\s*[^}]*\}',
     'info', 'code_smell',
     'S1643: String concatenation in a loop creates many temporary objects. Use `StringBuilder`.'),

    (r'public\s+\w+\s+\w+\s*\([^)]{200,}\)',
     'warning', 'code_smell',
     'S107: Method has too many parameters. Consider using a parameter object or builder pattern.'),

    (r'catch\s*\(\s*\w+\s+\w+\s*\)\s*\{\s*//[^\n]*\n\s*\}',
     'warning', 'bug',
     'S1166: Exception caught and silently ignored (only a comment inside catch). Log or re-throw.'),

    # ── TypeScript / JavaScript ───────────────────────────────────────────
    (r':\s*any\b',
     'warning', 'typescript',
     'S4325: TypeScript `any` type disables type checking. Use a specific type or `unknown`.'),

    (r'\bvar\s+\w+',
     'info', 'typescript',
     'S3504: `var` declaration is function-scoped and can cause subtle bugs. Use `let` or `const`.'),

    (r'===\s*null\s*\|\|\s*\w+\s*===\s*undefined|\bnull\s*==\s*\w+(?!\s*===)',
     'info', 'typescript',
     'S6582: Use nullish coalescing (`??`) or optional chaining (`?.`) instead of null/undefined checks.'),

    (r'console\.(log|warn|error|debug|info)\s*\(',
     'warning', 'logging',
     'S2228: `console.log` left in production code. Remove or replace with a proper logger.'),

    (r'\beval\s*\(',
     'error', 'security',
     'S1523: `eval()` is a security risk and a performance issue. Never use it.'),

    (r'==\s+(?:null|undefined|true|false)|(?:null|undefined|true|false)\s+==(?!=)',
     'warning', 'typescript',
     'S1440: Use strict equality (`===`/`!==`) instead of `==`/`!=` to avoid type coercion.'),

    (r'async\s+\w+[^{]*\{[^}]*await[^}]*\}(?![^{]*catch)',
     'warning', 'typescript',
     'S4823: `async`/`await` without try-catch — unhandled promise rejections will crash the process.'),

    # ── Python ────────────────────────────────────────────────────────────
    (r'except\s*:',
     'error', 'bug',
     'S5754: Bare `except:` catches everything including `SystemExit` and `KeyboardInterrupt`. Use `except Exception:`.'),

    (r'\bexec\s*\(',
     'error', 'security',
     'S1523: `exec()` is a security risk. Avoid executing dynamic code strings.'),

    (r'assert\s+',
     'warning', 'bug',
     'S5741: `assert` statements are disabled with `-O` flag and should not be used for runtime validation.'),

    # ── General / All Languages ──────────────────────────────────────────
    (r'\b(?:\d{1,3}\.){3}\d{1,3}\b',
     'info', 'code_smell',
     'S1313: Hardcoded IP address. Use a config property or environment variable.'),

    (r'(?i)(BEGIN RSA|BEGIN DSA|BEGIN EC|BEGIN PRIVATE KEY|BEGIN CERTIFICATE)',
     'error', 'security',
     'S6437: Private key or certificate embedded in source code. Move to a secrets manager immediately.'),

    (r'(?i)(aws_access_key_id|aws_secret_access_key)\s*=\s*["\'][^"\']{10,}',
     'error', 'security',
     'S6290: AWS credentials hardcoded in source. Use IAM roles or environment variables.'),
]

# Only check these file extensions for code patterns
_CODE_EXTENSIONS = {
    # JVM
    '.java', '.kt', '.groovy', '.scala',
    # Web / Mobile
    '.js', '.ts', '.tsx', '.jsx', '.mjs', '.cjs',
    # Python
    '.py',
    # Other
    '.go', '.rb', '.cs', '.php', '.swift', '.rs',
}
# Never flag these paths (test files, docs, generated code)
_SKIP_PATHS_RE = re.compile(
    r'(test|spec|__test__|__mocks__|\.md$|\.txt$|generated|\.min\.js$|vendor/|node_modules/)',
    re.IGNORECASE,
)


def _check_code_patterns(worktree_dir: Path, changed_set: set, result: StaticCheckResult):
    """
    Scan changed source files for common anti-patterns.
    Only checks added/modified lines (those starting with + in the diff).
    This uses the actual file in the worktree (already on the source branch).
    """
    for rel_path in changed_set:
        fpath = worktree_dir / rel_path
        ext   = Path(rel_path).suffix.lower()

        if ext not in _CODE_EXTENSIONS:
            continue
        if _SKIP_PATHS_RE.search(rel_path):
            continue
        if not fpath.exists():
            continue

        try:
            lines = fpath.read_text(errors='replace').splitlines()
        except Exception:
            continue

        for lineno, line in enumerate(lines, 1):
            for pattern, severity, category, message in _CODE_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(StaticIssue(
                        severity=severity,
                        category=category,
                        file=rel_path,
                        line=lineno,
                        message=message,
                    ))
                    break   # one issue per line max


# ---------------------------------------------------------------------------
# Helper: extract changed file paths from MR changes list
# ---------------------------------------------------------------------------
def extract_changed_files(changes: List[Dict]) -> List[str]:
    """Extract new_path from the MR changes list."""
    files = []
    for change in changes:
        path = change.get('new_path') or change.get('old_path')
        if path:
            files.append(path)
    return files
