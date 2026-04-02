"""MR Review Bot — main entry point."""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from typing import Tuple
import urllib3
from dotenv import load_dotenv

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from build_checker import BuildResult, check_mr_build
from static_checker import StaticCheckResult, extract_changed_files, run_static_checks
from gitlab_client import GitLabClient
from notifier import Notifier
from reviewer import AIReviewer

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / 'state.json'
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL_SECONDS', 600))    # 10 minutes
SNOOZE_INTERVAL = int(os.getenv('SNOOZE_INTERVAL_SECONDS', 120))  # 2 minutes


# ── State helpers ────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {'mrs': {}}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def mr_key(project_id, mr_iid) -> str:
    return f"{project_id}:{mr_iid}"


def parse_mr_key(key: str) -> Tuple[int, int]:
    parts = key.split(':')
    return int(parts[0]), int(parts[1])


# ── Poll cycle ───────────────────────────────────────────────────────────────

def poll_once(gl_client: GitLabClient, reviewer: AIReviewer, notifier: Notifier):
    username = os.getenv('GITLAB_USERNAME', 'z4743472')
    log.info(f"Polling open MRs for reviewer '{username}'...")

    mrs = gl_client.get_reviewer_mrs(username)
    log.info(f"Found {len(mrs)} open MR(s) assigned for review.")

    state = load_state()

    # Mark any previously tracked MRs that are no longer open as resolved
    # Covers merged, closed, or re-assigned MRs
    open_keys = {mr_key(mr.project_id, mr.iid) for mr in mrs}
    active_verdicts = {'NOTIFY_HUMAN', 'PENDING_REVIEW', 'WAITING_THREADS', 'SKIPPED_CONFLICTS'}
    for key, mr_state in state['mrs'].items():
        if key not in open_keys and mr_state.get('verdict') in active_verdicts:
            log.info(f"MR {key} is no longer open (merged/closed) — clearing from active tracking.")
            mr_state['verdict'] = 'RESOLVED_CLOSED'
    save_state(state)

    if not mrs:
        return

    for mr in mrs:
        key = mr_key(mr.project_id, mr.iid)
        mr_state = state['mrs'].get(key, {})
        current_sha = getattr(mr, 'sha', None)

        # ── Skip logic ────────────────────────────────────────────────────
        bot_disc_ids  = mr_state.get('bot_disc_ids', [])
        same_sha      = mr_state.get('last_reviewed_sha') == current_sha
        verdict_state = mr_state.get('verdict', '')

        # If we already reviewed this SHA and posted threads — check if dev resolved them
        if same_sha and verdict_state == 'REVIEW_POSTED':
            if not gl_client.are_bot_threads_resolved(full_mr, bot_disc_ids):
                log.info(f"MR {key}: waiting for developer to resolve {len(bot_disc_ids)} bot thread(s).")
                continue   # come back next poll
            else:
                log.info(f"MR {key}: all bot threads resolved — re-reviewing for merge.")
                # Fall through to re-review below

        # If already reviewed at this SHA and no pending threads — nothing to do
        elif same_sha and verdict_state not in ('WAITING_THREADS', 'SKIPPED_CONFLICTS'):
            log.debug(f"MR {key} already handled at SHA {(current_sha or '')[:8]}. Skipping.")
            continue

        log.info(f"Reviewing MR !{mr.iid} — \"{mr.title}\" (project {mr.project_id})")

        # Get full project-scoped MR object for API calls
        try:
            full_mr = gl_client.get_full_mr(mr.project_id, mr.iid)
        except Exception as e:
            log.error(f"Could not fetch full MR {key}: {e}")
            continue

        url = getattr(full_mr, 'web_url', '')

        # ── Pre-flight checks ──────────────────────────────────────────────

        # 1. Skip MRs with merge conflicts — post a comment once, keyed by MR key (not SHA)
        #    SHA can be None on conflict MRs, so we key on the MR itself
        if gl_client.has_merge_conflicts(full_mr):
            if not mr_state.get('conflict_comment_posted'):
                gl_client.post_note(
                    full_mr,
                    f"⚠️ **MR Review Bot**: This MR has merge conflicts and cannot be reviewed.\n\n"
                    f"Please resolve the conflicts and I will re-review automatically.\n\n"
                    f"🔗 {url}"
                )
                log.info(f"MR !{mr.iid} has merge conflicts — posted comment, skipping.")
            else:
                log.info(f"MR !{mr.iid} still has merge conflicts — skipping.")
            state['mrs'][key] = {
                **mr_state,
                'verdict': 'SKIPPED_CONFLICTS',
                'conflict_comment_posted': True,
                'title': mr.title,
                'url': url,
            }
            save_state(state)
            continue

        # 2. Check for unresolved threads from OTHER reviewers (not the bot's own)
        unresolved = gl_client.get_unresolved_threads(full_mr, exclude_disc_ids=bot_disc_ids)
        if unresolved:
            if mr_state.get('waiting_sha') != current_sha:
                # New SHA or first time — post a comment
                gl_client.post_note(
                    full_mr,
                    f"💬 **MR Review Bot**: Found {len(unresolved)} unresolved thread(s). "
                    f"I will start my review once all threads are resolved.\n\n"
                    f"🔗 {url}"
                )
                log.info(f"MR !{mr.iid} has {len(unresolved)} unresolved thread(s) — posted comment, waiting.")
            else:
                log.info(f"MR !{mr.iid} still has {len(unresolved)} unresolved thread(s) — waiting.")
            state['mrs'][key] = {
                **mr_state,
                'verdict': 'WAITING_THREADS',
                'waiting_sha': current_sha,
                'title': mr.title,
                'url': url,
            }
            save_state(state)
            continue

        # Conflicts are resolved — clear the flag so it re-notifies if conflicts return
        if mr_state.get('conflict_comment_posted'):
            mr_state.pop('conflict_comment_posted', None)

        # ── Local Build Check ──────────────────────────────────────────────
        # Clone/fetch the source branch and run ./gradlew compileJava locally.
        # - DEP_ERROR / SKIPPED / ERROR  → don't block, proceed to AI review
        # - CODE_ERROR (syntax, type mismatch, etc.) → post comment + block merge
        build_result: BuildResult = check_mr_build(full_mr, gl_client)
        log.info(f"Build check for MR !{mr.iid}: {build_result.status}")

        if build_result.has_code_errors:
            build_comment = build_result.as_comment_section()
            try:
                gl_client.post_note(full_mr, build_comment)
                log.warning(f"MR !{mr.iid} has compilation errors — posted build failure, blocking merge.")
            except Exception as e:
                log.error(f"Failed to post build error comment on MR {key}: {e}")
            _notify_and_record(
                notifier, state, key,
                title=mr.title,
                url=url,
                summary="Build FAILED — compilation errors found. See MR comments for details.",
                sha=current_sha,
            )
            save_state(state)
            continue   # skip AI review and merge for this MR

        # ── Static Checks (stack-agnostic) ────────────────────────────────
        # Duplicate keys, helm completeness, code patterns — runs on every MR
        worktree_dir = None
        try:
            from pathlib import Path as _Path
            from build_checker import _WORKSPACE, _safe_name
            worktree_dir = _WORKSPACE / f'{full_mr.project_id}-wt-{_safe_name(getattr(full_mr, "source_branch", ""))}'
        except Exception:
            pass

        # Accumulate all disc IDs posted this cycle (static + AI)
        all_posted_disc_ids: list = []

        if worktree_dir and worktree_dir.exists():
            try:
                changes       = gl_client.get_mr_changes(full_mr)
                changed_files = extract_changed_files(changes)
                static_result: StaticCheckResult = run_static_checks(worktree_dir, changed_files)
                if static_result.has_issues:
                    log.info(f"MR !{mr.iid}: {len(static_result.issues)} static issue(s) found")
                    static_comments = [i.as_comment() for i in static_result.issues]
                    try:
                        ids = gl_client.post_review_comments(
                            full_mr, static_comments,
                            summary=f"Static analysis: {len(static_result.issues)} issue(s) found.",
                        )
                        all_posted_disc_ids.extend(ids)
                    except Exception as e:
                        log.error(f"Failed to post static check comments on MR {key}: {e}")
                else:
                    log.info(f"MR !{mr.iid}: static checks passed ✅")
            except Exception as e:
                log.warning(f"Static checks failed for MR {key}: {e}")
        else:
            log.debug(f"MR !{mr.iid}: worktree not available — skipping static checks")

        # ── AI Review ─────────────────────────────────────────────────────

        try:
            result = reviewer.review(full_mr, gl_client)
        except Exception as e:
            log.error(f"AI review failed for MR {key}: {e}")
            _notify_and_record(
                notifier, state, key,
                title=mr.title,
                url=url,
                summary=f"AI review error: {e}",
                sha=current_sha,
            )
            save_state(state)
            continue

        verdict  = result.get('verdict', 'NOTIFY_HUMAN')
        summary  = result.get('summary', '')
        comments = result.get('comments', [])
        reasoning = result.get('reasoning', '')
        log.info(f"Verdict: {verdict} | {reasoning}")

        # Post inline threads — NEVER auto-resolve, developer must resolve them
        if comments or summary:
            try:
                ids = gl_client.post_review_comments(full_mr, comments, summary)
                all_posted_disc_ids.extend(ids)
            except Exception as e:
                log.error(f"Failed to post review comments on MR {key}: {e}", exc_info=True)

        if verdict == 'APPROVE_MERGE':
            if all_posted_disc_ids:
                # Threads were posted — wait for developer to resolve before merging
                log.info(
                    f"MR !{mr.iid}: APPROVE_MERGE but {len(all_posted_disc_ids)} thread(s) posted. "
                    f"Waiting for developer to resolve them before merging."
                )
                state['mrs'][key] = {
                    'last_reviewed_sha': current_sha,
                    'verdict': 'REVIEW_POSTED',
                    'bot_disc_ids': all_posted_disc_ids,
                    'reviewed_at': _now(),
                    'title': mr.title,
                    'url': url,
                    'summary': summary,
                }
            else:
                # No threads posted — clean MR, merge immediately
                try:
                    gl_client.approve_and_merge_mr(full_mr)
                    log.info(f"✅ Merged MR !{mr.iid} — no issues found")
                    state['mrs'][key] = {
                        'last_reviewed_sha': current_sha,
                        'verdict': 'APPROVED_MERGED',
                        'reviewed_at': _now(),
                        'title': mr.title,
                        'url': url,
                    }
                except Exception as e:
                    log.error(f"Approve/merge failed for MR {key} ({type(e).__name__}: {e})", exc_info=True)
                    _notify_and_record(
                        notifier, state, key,
                        title=mr.title, url=url,
                        summary=f"Auto-merge failed ({type(e).__name__}): {e}\n\n{summary}",
                        sha=current_sha,
                    )

        else:  # NOTIFY_HUMAN
            log.info(f"🔔 Notifying human for MR !{mr.iid}")
            # Save bot disc IDs so we don't re-flag the same issues after dev resolves
            state['mrs'][key] = {
                'last_reviewed_sha': current_sha,
                'verdict': 'NOTIFY_HUMAN',
                'bot_disc_ids': all_posted_disc_ids,
                'reviewed_at': _now(),
                'notified_at': _now(),
                'title': mr.title,
                'url': url,
                'summary': summary,
            }
            notifier.notify(mr.title, url, summary)

        save_state(state)



def _notify_and_record(notifier, state, key, title, url, summary, sha, snooze=False):
    notifier.notify(title, url, summary, snooze=snooze)
    state['mrs'][key] = {
        'last_reviewed_sha': sha,
        'verdict': 'NOTIFY_HUMAN',
        'reviewed_at': state.get('mrs', {}).get(key, {}).get('reviewed_at') or _now(),
        'notified_at': _now(),
        'title': title,
        'url': url,
        'summary': summary,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Snooze thread ─────────────────────────────────────────────────────────────

def snooze_loop(gl_client: GitLabClient, notifier: Notifier):
    """Re-notify every SNOOZE_INTERVAL seconds for unresolved MRs."""
    while True:
        time.sleep(SNOOZE_INTERVAL)
        try:
            _snooze_tick(gl_client, notifier)
        except Exception as e:
            log.error(f"Snooze tick error: {e}")


def _snooze_tick(gl_client: GitLabClient, notifier: Notifier):
    state = load_state()
    now = datetime.now(timezone.utc)
    changed = False

    for key, mr_state in state['mrs'].items():
        # Only snooze-notify for MRs explicitly waiting for human action
        # Never re-trigger on merged, closed, auto-approved, conflict-skipped, or thread-waiting MRs
        if mr_state.get('verdict') != 'NOTIFY_HUMAN':
            continue
        notified_at_str = mr_state.get('notified_at')
        if not notified_at_str:
            continue
        notified_at = datetime.fromisoformat(notified_at_str)
        elapsed = (now - notified_at).total_seconds()
        if elapsed < SNOOZE_INTERVAL:
            continue

        # Check if MR is still open before re-notifying
        try:
            project_id, mr_iid = parse_mr_key(key)
            current_state = gl_client.get_mr_state(project_id, mr_iid)
        except Exception as e:
            log.warning(f"Could not check MR state for {key}: {e}")
            current_state = 'opened'  # assume still open

        if current_state != 'opened':
            log.info(f"MR {key} is now '{current_state}', clearing pending notification.")
            mr_state['verdict'] = f'RESOLVED_{current_state.upper()}'
            changed = True
            continue

        log.info(f"⏰ Snooze re-notify for MR {key}: {mr_state.get('title','')}")
        notifier.notify(
            title=mr_state.get('title', 'MR needs your review'),
            url=mr_state.get('url', ''),
            summary=mr_state.get('summary', 'Still awaiting your review.'),
            snooze=True,
        )
        mr_state['notified_at'] = now.isoformat()
        changed = True

    if changed:
        save_state(state)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("Starting MR Review Bot...")
    log.info(f"  GitLab : {os.getenv('GITLAB_URL')}")
    log.info(f"  User   : {os.getenv('GITLAB_USERNAME', 'z4743472')}")
    log.info(f"  Poll   : every {POLL_INTERVAL}s ({POLL_INTERVAL // 60} min)")
    log.info(f"  Snooze : every {SNOOZE_INTERVAL}s ({SNOOZE_INTERVAL // 60} min)")

    gl_client = GitLabClient()
    reviewer  = AIReviewer()
    reviewer.gl_client = gl_client   # give reviewer access for file fetching
    notifier  = Notifier()

    # Background snooze checker
    t = threading.Thread(target=snooze_loop, args=(gl_client, notifier), daemon=True)
    t.start()

    # Main poll loop
    while True:
        try:
            poll_once(gl_client, reviewer, notifier)
        except Exception as e:
            log.error(f"Poll cycle error: {e}", exc_info=True)

        log.info(f"Sleeping {POLL_INTERVAL}s until next poll...")
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
