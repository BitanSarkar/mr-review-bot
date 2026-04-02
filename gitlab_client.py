"""GitLab API operations."""
import hashlib
import os
import re
import logging
import urllib3
import gitlab
from typing import Dict, List, Optional
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

SSL_VERIFY = os.getenv('GITLAB_SSL_VERIFY', 'false').lower() not in ('false', '0', 'no')
if not SSL_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class GitLabClient:
    def __init__(self):
        url = os.getenv('GITLAB_URL', '').rstrip('/')
        token = os.getenv('GITLAB_TOKEN')
        if not url or not token:
            raise ValueError("GITLAB_URL and GITLAB_TOKEN must be set in .env")
        self.gl = gitlab.Gitlab(url=url, private_token=token, ssl_verify=SSL_VERIFY, keep_base_url=True)
        self.gl.auth()
        log.info(f"Connected to GitLab at {url}")

    def get_reviewer_mrs(self, username: str) -> list:
        """Return all open MRs where `username` is a reviewer."""
        try:
            mrs = self.gl.mergerequests.list(
                state='opened',
                reviewer_username=username,
                scope='all',
                all=True,
            )
            return mrs
        except Exception as e:
            log.error(f"Failed to list MRs: {e}")
            return []

    def get_full_mr(self, project_id: int, mr_iid: int):
        """Get a full MR object with all methods available."""
        project = self.gl.projects.get(project_id)
        return project.mergerequests.get(mr_iid)

    def has_merge_conflicts(self, mr) -> bool:
        return getattr(mr, 'has_conflicts', False)

    def get_unresolved_threads(self, mr) -> list:
        """Return all unresolved discussion threads on the MR."""
        try:
            discussions = mr.discussions.list(all=True)
            unresolved = []
            for d in discussions:
                notes = d.attributes.get('notes', [])
                resolvable = [n for n in notes if n.get('resolvable')]
                if resolvable and not all(n.get('resolved') for n in resolvable):
                    unresolved.append(d)
            return unresolved
        except Exception as e:
            log.warning(f"Could not fetch discussions for MR !{mr.iid}: {e}")
            return []

    def resolve_thread(self, mr, discussion_id: str):
        try:
            discussion = mr.discussions.get(discussion_id)
            discussion.resolved = True
            discussion.save()
        except Exception as e:
            log.warning(f"Could not resolve thread {discussion_id}: {e}")

    def resolve_all_threads(self, mr):
        for d in self.get_unresolved_threads(mr):
            self.resolve_thread(mr, d.id)

    # ── Diff / changes ────────────────────────────────────────────────────────

    def get_mr_changes(self, mr) -> List[Dict]:
        """Return raw changes list from the MR."""
        try:
            return mr.changes().get('changes', [])
        except Exception as e:
            log.error(f"Failed to get MR changes: {e}")
            return []

    def get_mr_diff(self, mr) -> str:
        """Return a formatted unified diff string annotated with line numbers."""
        changes = self.get_mr_changes(mr)
        if not changes:
            return ''
        parts = []
        for change in changes:
            old_path = change.get('old_path', '')
            new_path = change.get('new_path', '')
            raw_diff = change.get('diff', '')
            if change.get('new_file'):
                header = f"--- /dev/null\n+++ b/{new_path}"
            elif change.get('deleted_file'):
                header = f"--- a/{old_path}\n+++ /dev/null"
            else:
                header = f"--- a/{old_path}\n+++ b/{new_path}"
            # Annotate each line with its new-file line number
            annotated = _annotate_diff_lines(raw_diff)
            parts.append(f"{header}\n{annotated}")
        return '\n\n'.join(parts)

    def get_file_content(self, mr, file_path: str) -> Optional[str]:
        """
        Fetch the full content of a file at the MR's head SHA.
        Returns the file content with line numbers, or None on failure.
        """
        try:
            refs = self.get_diff_refs(mr)
            ref = refs['head_sha'] if refs else getattr(mr, 'sha', 'HEAD')
            project = self.gl.projects.get(mr.project_id)
            f = project.files.get(file_path=file_path, ref=ref)
            import base64
            content = base64.b64decode(f.content).decode('utf-8', errors='replace')
            # Add line numbers so the model can cite them accurately
            numbered = '\n'.join(
                f"[L{i+1:>4}] {line}"
                for i, line in enumerate(content.splitlines())
            )
            return numbered
        except Exception as e:
            log.warning(f"Could not fetch file {file_path} for MR !{mr.iid}: {e}")
            return None

    def get_diff_refs(self, mr) -> Optional[Dict]:
        """Return diff_refs dict with base_sha, start_sha, head_sha."""
        refs = getattr(mr, 'diff_refs', None)
        if isinstance(refs, dict) and refs.get('base_sha') and refs.get('head_sha'):
            return refs
        return None

    # ── Posting comments ──────────────────────────────────────────────────────

    def post_note(self, mr, body: str):
        """Post a general MR-level note (used for summary)."""
        mr.notes.create({'body': body})
        log.info(f"Posted summary note on MR !{mr.iid}")

    def _post_inline_thread_raw(self, mr, diff_refs: Dict,
                                file_path: str, new_line: int,
                                old_line: Optional[int], body: str):
        """
        Post an inline discussion thread. Returns the discussion object on
        success, None on failure. Includes line_code for older GitLab instances.
        """
        line_code = _compute_line_code(file_path, old_line, new_line)
        try:
            return mr.discussions.create({
                'body': body,
                'position': {
                    'position_type': 'text',
                    'base_sha':  diff_refs['base_sha'],
                    'start_sha': diff_refs.get('start_sha') or diff_refs['base_sha'],
                    'head_sha':  diff_refs['head_sha'],
                    'new_path':  file_path,
                    'old_path':  file_path,
                    'new_line':  new_line,
                    'old_line':  old_line,   # None for pure additions, int for context lines
                    'line_code': line_code,  # required by older GitLab servers
                },
            })
        except Exception as e:
            log.warning(f"Inline thread failed ({file_path}:{new_line}): {e}")
            return None

    def get_bot_username(self) -> str:
        """Return the authenticated user's username."""
        try:
            return self.gl.auth() or self.gl.users.get(self.gl.user.id).username
        except Exception:
            return os.getenv('GITLAB_USERNAME', '')

    def are_bot_threads_resolved(self, mr, disc_ids: List[str]) -> bool:
        """Return True if ALL of the given discussion IDs are now resolved."""
        if not disc_ids:
            return True
        try:
            discussions = mr.discussions.list(all=True)
            disc_map = {d.id: d for d in discussions}
            for disc_id in disc_ids:
                d = disc_map.get(str(disc_id))
                if not d:
                    continue   # discussion deleted — treat as resolved
                notes = d.attributes.get('notes', [])
                resolvable = [n for n in notes if n.get('resolvable')]
                if resolvable and not all(n.get('resolved') for n in resolvable):
                    return False
            return True
        except Exception as e:
            log.warning(f"Could not check bot thread resolution status: {e}")
            return False   # assume not resolved if we can't check

    def get_unresolved_threads(self, mr, exclude_disc_ids: Optional[List[str]] = None) -> list:
        """
        Return all unresolved discussion threads on the MR.
        exclude_disc_ids — skip threads created by the bot (already tracked separately).
        """
        exclude = set(str(i) for i in (exclude_disc_ids or []))
        try:
            discussions = mr.discussions.list(all=True)
            unresolved = []
            for d in discussions:
                if str(d.id) in exclude:
                    continue   # this is our own thread — skip
                notes = d.attributes.get('notes', [])
                resolvable = [n for n in notes if n.get('resolvable')]
                if resolvable and not all(n.get('resolved') for n in resolvable):
                    unresolved.append(d)
            return unresolved
        except Exception as e:
            log.warning(f"Could not fetch discussions for MR !{mr.iid}: {e}")
            return []

    def post_review_comments(self, mr, comments: List[Dict], summary: str) -> List[str]:
        """
        Post all AI/static review comments as inline threads where possible,
        falling back to a single summary note for anything that can't be inlined.
        Comments with a 'suggestion' field get a GitLab suggestion block.

        Returns the list of created discussion IDs (so the bot can track them in state
        and wait for the developer to resolve them before merging).
        Threads are NEVER auto-resolved — that is the developer's responsibility.
        """
        diff_refs  = self.get_diff_refs(mr)
        changes    = self.get_mr_changes(mr)
        line_map   = _build_line_map(changes)
        fallback_lines: List[str] = []
        inline_count = 0
        posted_disc_ids: List[str] = []

        for c in comments:
            file_path  = c.get('file') or None
            line_num   = c.get('line') or None
            body       = (c.get('body') or '').strip()
            suggestion = (c.get('suggestion') or '').strip()

            if not body:
                continue

            comment_body = body
            if suggestion:
                comment_body += f"\n\n```suggestion\n{suggestion}\n```"

            if diff_refs and file_path and line_num:
                old_line = line_map.get(file_path, {}).get(line_num)
                disc = self._post_inline_thread_raw(
                    mr, diff_refs, file_path, line_num, old_line, comment_body
                )
                if disc:
                    inline_count += 1
                    posted_disc_ids.append(str(disc.id))
                    continue

            location = f"`{file_path}` line {line_num}" if file_path and line_num else (file_path or 'General')
            fallback_lines.append(f"**{location}**\n{comment_body}")

        log.info(f"MR !{mr.iid}: posted {inline_count} inline thread(s), "
                 f"{len(fallback_lines)} fallback comment(s)")

        # Always post a summary note
        note_parts = [f"## 🤖 AI Code Review\n\n**Summary:** {summary}"]
        if fallback_lines:
            note_parts.append("### Additional Comments\n\n" + "\n\n---\n\n".join(fallback_lines))
        if posted_disc_ids:
            note_parts.append(
                f"💬 **{len(posted_disc_ids)} inline thread(s) posted.** "
                f"Please resolve each thread — I will re-review and merge once all are resolved."
            )
        note_parts.append(f"---\n_Reviewed by MR Review Bot ({os.getenv('OLLAMA_MODEL','ollama')})_")
        mr.notes.create({'body': '\n\n'.join(note_parts)})

        return posted_disc_ids

    # ── MR actions ────────────────────────────────────────────────────────────

    def approve_and_merge_mr(self, mr):
        """
        Merge the MR directly (maintainer access — no explicit approval step).
        Approve is skipped intentionally; PAT needs only read_api + write on MRs.
        """
        try:
            mr.merge(should_remove_source_branch=False, merge_when_pipeline_succeeds=False)
            log.info(f"Merged MR !{mr.iid} ✅")
        except gitlab.exceptions.GitlabMRClosedError:
            log.warning(f"MR !{mr.iid} is already closed/merged")
        except Exception as e:
            raise RuntimeError(f"Merge failed for MR !{mr.iid}: {e}") from e

    def get_mr_state(self, project_id: int, mr_iid: int) -> str:
        try:
            return self.get_full_mr(project_id, mr_iid).state
        except Exception as e:
            log.error(f"Failed to get MR state for {project_id}:{mr_iid}: {e}")
            return 'unknown'


# ── Diff helpers ──────────────────────────────────────────────────────────────

def _compute_line_code(file_path: str, old_line: Optional[int], new_line: Optional[int]) -> str:
    """
    Compute GitLab's line_code: sha1(file_path)_{old_line}_{new_line}
    Required by older GitLab servers alongside the position dict.
    Added lines  → old_line=0
    Removed lines → new_line=0
    Context lines → both set
    """
    file_hash = hashlib.sha1(file_path.encode()).hexdigest()
    return f"{file_hash}_{old_line or 0}_{new_line or 0}"


def _build_line_map(changes: list) -> dict:
    """
    Build {file_path: {new_line: old_line}} from MR changes list.
    - Added lines   → old_line is None
    - Context lines → old_line is the old-file line number
    """
    line_map: dict = {}
    for change in changes:
        new_path = change.get('new_path', '')
        raw_diff = change.get('diff', '')
        if not new_path or not raw_diff:
            continue
        file_map: dict = {}
        new_line = 0
        old_line = 0
        for line in raw_diff.splitlines():
            if line.startswith('@@'):
                m_new = re.search(r'\+(\d+)', line)
                m_old = re.search(r'-(\d+)', line)
                if m_new:
                    new_line = int(m_new.group(1)) - 1
                if m_old:
                    old_line = int(m_old.group(1)) - 1
            elif line.startswith('+'):
                new_line += 1
                file_map[new_line] = None        # added — no old_line
            elif line.startswith('-'):
                old_line += 1                    # removed — no new_line entry
            elif not line.startswith('\\'):
                new_line += 1
                old_line += 1
                file_map[new_line] = old_line    # context — both exist
        line_map[new_path] = file_map
    return line_map


def _annotate_diff_lines(raw_diff: str) -> str:
    """
    Annotate each line of a file diff with both old and new file line numbers.
    Format:
      [N42+ O   ]  added line    — new file line 42, no old-file line
      [N43  O43 ]  context line  — same line number in both files
      [N    O44-]  removed line  — old file line 44, not in new file
    This lets the model cite precise line numbers in its comments.
    """
    lines = raw_diff.splitlines()
    result = []
    new_line = 0
    old_line = 0

    for line in lines:
        if line.startswith('@@'):
            m_new = re.search(r'\+(\d+)', line)
            m_old = re.search(r'-(\d+)', line)
            if m_new:
                new_line = int(m_new.group(1)) - 1
            if m_old:
                old_line = int(m_old.group(1)) - 1
            result.append(line)
        elif line.startswith('+'):
            new_line += 1
            result.append(f"[N{new_line:>4}+ O    ] {line[1:]}")
        elif line.startswith('-'):
            old_line += 1
            result.append(f"[N     O{old_line:>4}-] {line[1:]}")
        elif line.startswith('\\'):
            result.append(line)
        else:
            new_line += 1
            old_line += 1
            result.append(f"[N{new_line:>4}  O{old_line:>4} ] {line[1:] if line.startswith(' ') else line}")

    return '\n'.join(result)
