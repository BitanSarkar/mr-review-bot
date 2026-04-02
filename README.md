# MR Review Bot

Automated GitLab MR reviewer for Omantel microservices. Polls for MRs assigned to you as reviewer, runs a local build check, static analysis, and an AI code review — then either merges or notifies you based on the outcome.

---

## How it works

### Poll cycle (every 10 minutes)

1. **Fetch open MRs** where you are a reviewer
2. **Skip** MRs that are merged/closed — clear them from tracking
3. **Conflict check** — if the MR has merge conflicts, post a thread and skip. Re-reviews automatically once conflicts are resolved
4. **Other reviewers' threads** — if there are unresolved threads from other people, wait until they're resolved before reviewing
5. **Local build check** — clones the source branch, auto-detects the stack, runs the appropriate compile command:

   | Stack detected | Command run |
   |---|---|
   | Gradle (`gradlew` / `build.gradle`) | `./gradlew compileJava compileKotlin` |
   | Maven (`pom.xml`) | `mvn compile` |
   | TypeScript (`tsconfig.json`) | `npx tsc --noEmit` |
   | Node.js (`package.json`) | `npm run build` / `npm run lint` |
   | Python | `python3 -m compileall` |
   | Unknown / tool not installed | Skipped cleanly |

   - **Dependency/lib resolution errors** → ignored, does not block
   - **Real compilation errors** (syntax, type mismatch, etc.) → posted as comment, merge blocked
   - **IBM/WAS or unknown stack** → build skipped, review still runs

6. **Static analysis** — runs on every MR regardless of stack:
   - Duplicate keys in `application.yaml`, `application-*.yaml`, `.env`, `.properties`
   - Helm completeness — every `${VAR}` in `application.yaml` must exist in `values.yaml`
   - Sonar-equivalent code patterns (see below)

7. **AI review** — `qwen2.5-coder:14b` via Ollama (runs fully locally):
   - Large diffs split by file into batches, each reviewed independently
   - If uncertain about a specific file → fetches full file from GitLab for a second pass
   - Posts inline threads with optional code suggestions on specific lines

8. **Verdict**:
   - `APPROVE_MERGE` + no threads → **merges immediately** ✅
   - `APPROVE_MERGE` + threads posted → **waits for developer to resolve all threads**, then re-reviews and merges
   - `NOTIFY_HUMAN` → sends you a **macOS notification** with a dialog to open the MR

9. **Thread lifecycle**:
   - Bot **never auto-resolves** its own threads
   - Developer must resolve each thread
   - Once all bot threads are resolved → bot re-reviews automatically
   - Source branch is **never deleted** after merge

10. **Snooze** — if you don't act on a notification, it re-notifies every 2 minutes until the MR is closed or merged

---

## Static analysis rules (Sonar-equivalent)

| Category | What's checked |
|---|---|
| **Reactive** | `.block()`, `.blockFirst/Last()`, `Thread.sleep`, `.subscribe()` in service layer, `.toFuture().get()` |
| **Security** | Hardcoded credentials, `new Random()` (S2245), MD5/SHA-1 (S4790), deserialization (S5135), command injection (S2076), XXE (S2755), CORS `*` (S5122), plain HTTP (S5332), insecure cookies, SSL disabled, AWS keys, embedded private keys |
| **SQL Injection** | String concatenation in queries (S2077), dynamic `@Query` |
| **Bugs** | String `==` comparison (S4973), empty catch (S1166), `return` in `finally` (S1143), float equality (S1244), deprecated boxed constructors (S2129) |
| **Spring Boot** | Field `@Autowired` (S4175), `@Transactional` on private method (S2229), generic `@RequestMapping` (S4488), JUnit 4 in JUnit 5 project (S5786) |
| **Logging** | `System.out`/`System.err` (S106), `printStackTrace` (S1148), string concat in log messages (S2629), sensitive data in logs (S5145) |
| **Code Smells** | TODO/FIXME (S1134), `@SuppressWarnings` (S1309), deep nesting (S134), `StringBuffer` (S1149), string concat in loop (S1643), too many parameters (S107) |
| **TypeScript/JS** | `any` type (S4325), `var` (S3504), `eval()` (S1523), `==` instead of `===` (S1440), unhandled `async/await` (S4823) |
| **Python** | Bare `except:` (S5754), `exec()`, `assert` in runtime code (S5741) |

Test files, generated code, and `node_modules` are excluded from pattern checks.

---

## Custom review prompt

Edit `prompt.md` to adjust the review guidelines. It already contains Omantel-specific rules for:
- Reactive programming (Mono/Flux, Project Reactor)
- Spring Boot architecture patterns
- Exception hierarchy (`GlobalAppException`, `APIException`, etc.)
- Security (LTPA2, Cognito, `@ValidateServiceToken`)
- Oracle JPA + DynamoDB patterns
- Redis caching via `ot-cache-manager`
- Feign client timeouts and circuit breakers
- Resilience4j retry/circuit breaker rules
- Logging and observability
- Jakarta validation
- API design and versioning

---

## Setup

```bash
# 1. Enter the folder
cd mr-review-bot

# 2. Create and activate venv
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env — fill in GITLAB_URL, GITLAB_TOKEN, GITLAB_USERNAME

# 5. Start Ollama as a background service
brew services start ollama
ollama pull qwen2.5-coder:14b

# 6. Run the bot
python bot.py
```

---

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `GITLAB_URL` | — | `https://gitlab.omantel.om:18015` |
| `GITLAB_TOKEN` | — | Personal Access Token (needs `api` scope) |
| `GITLAB_USERNAME` | — | Your GitLab username (`z4743472`) |
| `GITLAB_SSL_VERIFY` | `false` | Set to `true` if your GitLab cert is trusted |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `qwen2.5-coder:14b` | Model for AI review |
| `OLLAMA_NUM_CTX` | `16384` | Context window (tokens) |
| `OLLAMA_TIMEOUT_SECONDS` | `600` | Max time per review batch |
| `POLL_INTERVAL_SECONDS` | `600` | How often to poll GitLab |
| `SNOOZE_INTERVAL_SECONDS` | `120` | Re-notification interval |
| `BUILD_WORKSPACE` | `/tmp/mr-review-bot` | Where repos are cloned for build checks |
| `BUILD_TIMEOUT_SECONDS` | `300` | Max build time before skipping |

---

## File structure

```
bot.py              — Main polling loop + snooze thread
reviewer.py         — AI review (Ollama, batched, two-pass with file fetch)
gitlab_client.py    — GitLab API (inline threads, merge, thread tracking)
build_checker.py    — Multi-stack local build check
static_checker.py   — Stack-agnostic static analysis (Sonar-equivalent rules)
notifier.py         — macOS notifications + dialog via osascript
prompt.md           — Omantel-specific review guidelines (edit this)
list_mrs.py         — Utility: list your open review MRs with correct URLs
state.json          — Runtime state (auto-created, gitignored)
.env                — Your secrets (gitignored)
.env.example        — Template for .env
```

---

## Useful commands

```bash
# List all open MRs assigned to you (with correct port in URLs)
python list_mrs.py

# Force re-review of all MRs on next poll
echo '{"mrs": {}}' > state.json

# Check Ollama is running
curl http://localhost:11434/api/tags
```
