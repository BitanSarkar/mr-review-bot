"""AI-powered MR review using Ollama (local LLM).

Large diffs are split by file and reviewed in batches.
Results are aggregated — all comments collected, verdict is APPROVE_MERGE
only if every batch passes cleanly.
"""
import os
import json
import logging
import re
from pathlib import Path
from typing import List, Dict, Any
import ollama
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)

PROMPT_FILE = Path(__file__).parent / 'prompt.md'
CHUNK_SIZE   = 40_000   # chars per batch — safe for 14b with 16k ctx
MAX_CHUNKS   = 10       # never send more than 10 batches per MR


class AIReviewer:
    def __init__(self):
        self.model       = os.getenv('OLLAMA_MODEL', 'qwen2.5-coder:14b')
        self.ollama_host = os.getenv('OLLAMA_HOST', 'http://localhost:11434')
        self.num_ctx     = int(os.getenv('OLLAMA_NUM_CTX', 16384))
        self.timeout     = int(os.getenv('OLLAMA_TIMEOUT_SECONDS', 600))
        self.client      = ollama.Client(host=self.ollama_host, timeout=self.timeout)
        self.custom_prompt = PROMPT_FILE.read_text() if PROMPT_FILE.exists() else ''
        self.gl_client   = None   # injected by bot.py after construction
        log.info(f"AI Reviewer: {self.model} @ {self.ollama_host} "
                 f"(ctx={self.num_ctx}, timeout={self.timeout}s, chunk={CHUNK_SIZE})")

    # ── Public API ────────────────────────────────────────────────────────────

    def review(self, mr, gl_client) -> Dict[str, Any]:
        """
        Review an MR — splits large diffs into per-file batches and aggregates.
        Returns:
            {
                "verdict":  "APPROVE_MERGE" | "NOTIFY_HUMAN",
                "summary":  str,
                "comments": [{"file": str|None, "line": int|None, "body": str}],
                "reasoning": str
            }
        """
        diff = gl_client.get_mr_diff(mr)
        if not diff:
            log.warning(f"No diff for MR !{mr.iid} — defaulting to NOTIFY_HUMAN")
            return self._empty_result("Could not retrieve diff for review.")

        # Split diff into per-file sections then group into batches
        file_diffs = self._split_by_file(diff)
        batches    = self._group_into_batches(file_diffs)
        total      = len(batches)

        log.info(f"MR !{mr.iid}: {len(file_diffs)} file(s) → {total} batch(es) to review")

        all_comments: List[Dict] = []
        batch_verdicts: List[str] = []
        batch_summaries: List[str] = []

        for idx, batch_diff in enumerate(batches, 1):
            log.info(f"  Batch {idx}/{total} ({len(batch_diff)} chars)...")
            result = self._review_batch(mr, batch_diff, idx, total)
            all_comments.extend(result.get('comments', []))
            batch_verdicts.append(result.get('verdict', 'NOTIFY_HUMAN'))
            batch_summaries.append(result.get('summary', ''))

        # Aggregate: APPROVE_MERGE only if every single batch passed
        final_verdict = (
            'APPROVE_MERGE'
            if all(v == 'APPROVE_MERGE' for v in batch_verdicts)
            else 'NOTIFY_HUMAN'
        )
        final_summary = self._aggregate_summary(batch_summaries, final_verdict, total)

        log.info(f"MR !{mr.iid}: final verdict = {final_verdict} "
                 f"({batch_verdicts.count('APPROVE_MERGE')}/{total} batches passed)")

        return {
            'verdict':   final_verdict,
            'summary':   final_summary,
            'comments':  all_comments,
            'reasoning': f"{batch_verdicts.count('APPROVE_MERGE')}/{total} batch(es) approved.",
        }

    def format_comments_as_note(self, comments: list, summary: str) -> str:
        """Format AI review output into a single MR note."""
        lines = [
            '## 🤖 AI Code Review',
            '',
            f'**Summary:** {summary}',
            '',
        ]
        if comments:
            lines.append('### Comments')
            lines.append('')
            for c in comments:
                location = ''
                if c.get('file'):
                    location = f"`{c['file']}`"
                    if c.get('line'):
                        location += f" line {c['line']}"
                    location = f'**{location}** — '
                lines.append(f"- {location}{c['body']}")
        lines += ['', '---', f'_Reviewed by MR Review Bot ({self.model})_']
        return '\n'.join(lines)

    # ── Diff splitting ────────────────────────────────────────────────────────

    def _split_by_file(self, diff: str) -> List[str]:
        """Split a combined diff into individual per-file diffs."""
        # Each file diff starts with "--- " or "diff --git"
        parts = re.split(r'(?=^--- )', diff, flags=re.MULTILINE)
        return [p.strip() for p in parts if p.strip()]

    def _group_into_batches(self, file_diffs: List[str]) -> List[str]:
        """
        Group file diffs into batches that each fit within CHUNK_SIZE chars.
        A single file that exceeds CHUNK_SIZE gets its own batch (not split further).
        """
        batches: List[str] = []
        current_parts: List[str] = []
        current_size = 0

        for fd in file_diffs:
            if current_size + len(fd) > CHUNK_SIZE and current_parts:
                batches.append('\n\n'.join(current_parts))
                current_parts = []
                current_size  = 0
            current_parts.append(fd)
            current_size += len(fd)

        if current_parts:
            batches.append('\n\n'.join(current_parts))

        # Hard cap — if somehow we exceed MAX_CHUNKS, merge the tail
        if len(batches) > MAX_CHUNKS:
            log.warning(f"Too many batches ({len(batches)}), capping at {MAX_CHUNKS}")
            tail = '\n\n'.join(batches[MAX_CHUNKS - 1:])
            batches = batches[:MAX_CHUNKS - 1] + [tail]

        return batches

    # ── Single-batch review ───────────────────────────────────────────────────

    def _review_batch(self, mr, batch_diff: str, batch_num: int, total: int) -> Dict[str, Any]:
        """
        Two-pass review for one batch:
          Pass 1 — review the diff. If model is uncertain about specific files,
          Pass 2 — fetch those full files from GitLab and re-review with full context.
        """
        system = self._build_system_message()

        # Pass 1: diff only
        messages = [
            {'role': 'system', 'content': system},
            {'role': 'user',   'content': self._build_user_message(mr, batch_diff, batch_num, total)},
        ]
        raw = self._stream_response(mr.iid, messages, batch_num, total, pass_num=1)
        result = self._parse_response(raw)

        # If the model wants more context, fetch the files and do pass 2
        needs_context = result.pop('needs_context', [])
        if needs_context and result.get('verdict') == 'NOTIFY_HUMAN':
            log.info(f"  MR !{mr.iid} batch {batch_num}/{total}: "
                     f"model uncertain — fetching {len(needs_context)} file(s) for context...")
            file_sections = []
            for item in needs_context[:5]:   # cap at 5 files to avoid huge context
                fpath  = item.get('file') if isinstance(item, dict) else str(item)
                reason = item.get('reason', '') if isinstance(item, dict) else ''
                content = self.gl_client.get_file_content(mr, fpath) if self.gl_client else None
                if content:
                    file_sections.append(
                        f"### Full file: `{fpath}`\n"
                        + (f"_(Reason for fetching: {reason})_\n\n" if reason else "")
                        + f"```java\n{content}\n```"
                    )
                    log.info(f"    Fetched {fpath} for pass 2")
                else:
                    log.warning(f"    Could not fetch {fpath}")

            if file_sections:
                pass2_user = (
                    self._build_user_message(mr, batch_diff, batch_num, total)
                    + "\n\n---\n\n## Full File Context (fetched because you were uncertain)\n\n"
                    + "\n\n".join(file_sections)
                    + "\n\nNow re-evaluate with this full context and give your final verdict."
                )
                messages2 = [
                    {'role': 'system', 'content': system},
                    {'role': 'user',   'content': pass2_user},
                ]
                raw2 = self._stream_response(mr.iid, messages2, batch_num, total, pass_num=2)
                result2 = self._parse_response(raw2)
                result2.pop('needs_context', None)
                # Merge comments from both passes (pass2 supersedes verdict)
                all_comments = result.get('comments', []) + result2.get('comments', [])
                result2['comments'] = all_comments
                log.info(f"  MR !{mr.iid} batch {batch_num}/{total}: "
                         f"pass 2 verdict = {result2.get('verdict')}")
                return result2

        result.pop('needs_context', None)
        return result

    def _stream_response(self, mr_iid: int, messages: list,
                         batch_num: int = 1, total: int = 1, pass_num: int = 1) -> str:
        """Stream tokens from Ollama, logging progress every 100 tokens."""
        chunks: List[str] = []
        token_count = 0
        pass_label = f"pass{pass_num} " if pass_num > 1 else ""
        stream = self.client.chat(
            model=self.model,
            messages=messages,
            stream=True,
            options={
                'temperature': 0.1,
                'num_ctx': self.num_ctx,
            },
        )
        for chunk in stream:
            token = chunk.message.content or ''
            chunks.append(token)
            token_count += 1
            if token_count % 100 == 0:
                log.info(f"    MR !{mr_iid} batch {batch_num}/{total} {pass_label}"
                         f"{token_count} tokens so far...")
        log.info(f"    MR !{mr_iid} batch {batch_num}/{total} {pass_label}"
                 f"done ({token_count} tokens)")
        return ''.join(chunks)

    # ── Prompt builders ───────────────────────────────────────────────────────

    def _build_system_message(self) -> str:
        return f"""You are an expert code reviewer integrated into a GitLab MR review automation system.

Before reviewing the diff, read the MR title, description, labels, and branch names to understand:
- What feature/bug/task this MR is addressing
- The intended scope and purpose of the change
- Any linked tickets or milestones that provide additional context

Use this understanding to judge whether the code correctly implements what was intended.

**On logic changes:** A changed condition, flow, or return value is NOT suspicious on its own.
Developers change logic to fix bugs or build features — that is expected.
Only flag a logic change if you can clearly explain what will go wrong at runtime given the
stated intent of this MR. If you are unsure, fetch the full file via needs_context first.

{self.custom_prompt}

---

CRITICAL: Respond with ONLY valid JSON. No markdown fences, no text before or after. Start with {{ end with }}.

Response schema:
{{
  "verdict": "APPROVE_MERGE" or "NOTIFY_HUMAN",
  "summary": "<one or two sentence assessment>",
  "comments": [
    {{
      "file": "<exact file path from the diff>",
      "line": <N line number from [N42+...] or [N43 ...] annotation — always the new-file line>,
      "body": "<specific comment explaining the issue>",
      "suggestion": "<optional: exact replacement code for that line>"
    }}
  ],
  "needs_context": [
    {{
      "file": "<file path you need to see in full>",
      "reason": "<one sentence: what specifically you are unsure about>"
    }}
  ],
  "reasoning": "<why you chose this verdict>"
}}

Rules:
- Comment on every issue you find — secrets, .block(), wrong patterns, bugs
- APPROVE_MERGE + comments is the normal outcome for an MR with minor issues
- Only set verdict=NOTIFY_HUMAN if you genuinely cannot judge correctness even after reading the diff
- If unsure about a specific file, add it to needs_context — the full file will be fetched and you will get a second pass
- needs_context should be [] if you are confident in your verdict
- Output ONLY the JSON object"""

    def _build_user_message(self, mr, diff: str, batch_num: int, total: int) -> str:
        description = getattr(mr, 'description', '') or 'No description provided.'

        author = 'Unknown'
        if isinstance(getattr(mr, 'author', None), dict):
            author = mr.author.get('name') or mr.author.get('username', 'Unknown')

        labels    = ', '.join(getattr(mr, 'labels', []) or []) or 'None'
        milestone = 'None'
        if isinstance(getattr(mr, 'milestone', None), dict):
            milestone = mr.milestone.get('title', 'None')

        batch_note = (
            f'> 📦 **Batch {batch_num} of {total}** — reviewing a subset of files.\n\n'
            if total > 1 else ''
        )

        return f"""Please review the following Merge Request diff.

## MR Context
**Title:** {mr.title}
**Author:** {author}
**Source branch:** {getattr(mr, 'source_branch', 'unknown')}
**Target branch:** {getattr(mr, 'target_branch', 'unknown')}
**Labels:** {labels}
**Milestone:** {milestone}
**URL:** {getattr(mr, 'web_url', '')}

## Description
{description}

## Diff
{batch_note}```diff
{diff}
```

Respond with JSON only."""

    # ── Response parsing ──────────────────────────────────────────────────────

    def _parse_response(self, raw: str) -> Dict[str, Any]:
        text = raw.strip()
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            text = match.group(0)
        try:
            data = json.loads(text)
            if data.get('verdict') not in ('APPROVE_MERGE', 'NOTIFY_HUMAN'):
                data['verdict'] = 'NOTIFY_HUMAN'
            data.setdefault('summary', 'Review completed.')
            data.setdefault('comments', [])
            data.setdefault('reasoning', '')
            return data
        except json.JSONDecodeError as e:
            log.error(f"JSON parse error: {e}\nRaw: {raw[:500]}")
            return {
                'verdict':   'NOTIFY_HUMAN',
                'summary':   'AI review completed but response could not be parsed.',
                'comments':  [{'file': None, 'line': None,
                                'body': f'Raw AI output:\n{raw[:2000]}'}],
                'reasoning': 'JSON parse error — defaulting to human review.',
            }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _empty_result(self, reason: str) -> Dict[str, Any]:
        return {
            'verdict':   'NOTIFY_HUMAN',
            'summary':   reason,
            'comments':  [],
            'reasoning': reason,
        }

    def _aggregate_summary(self, summaries: List[str], verdict: str, total: int) -> str:
        if total == 1:
            return summaries[0] if summaries else 'Review completed.'
        intro = (
            f'Reviewed in {total} batch(es). '
            + ('All batches passed — safe to merge.'
               if verdict == 'APPROVE_MERGE'
               else 'One or more batches require attention.')
        )
        unique = list(dict.fromkeys(s for s in summaries if s))  # dedupe, preserve order
        return intro + ' ' + ' | '.join(unique[:5])
