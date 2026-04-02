"""Quick script to list all open MRs where you are a reviewer."""
import urllib3
urllib3.disable_warnings()

from dotenv import load_dotenv
load_dotenv()

from gitlab_client import GitLabClient
from urllib.parse import urlparse
import os

gl = GitLabClient()
mrs = gl.get_reviewer_mrs(os.getenv('GITLAB_USERNAME', 'z4743472'))

# GitLab server strips the port from web_url — rewrite it using the configured URL
_gitlab_url = os.getenv('GITLAB_URL', '').rstrip('/')
_configured = urlparse(_gitlab_url)
_server_base = f"{_configured.scheme}://gitlab.omantel.om"          # what server returns
_correct_base = f"{_configured.scheme}://gitlab.omantel.om:{_configured.port}"  # what we want

def fix_url(url: str) -> str:
    return url.replace(_server_base, _correct_base) if url else url

if not mrs:
    print("No open MRs found.")
else:
    print(f"\n{'#':<6} {'PROJECT':<10} {'AUTHOR':<20} {'SOURCE → TARGET':<45} TITLE")
    print("-" * 130)
    for mr in sorted(mrs, key=lambda m: m.project_id):
        author = mr.author.get('name') or mr.author.get('username', '?') if isinstance(mr.author, dict) else '?'
        branch = f"{mr.source_branch} → {mr.target_branch}"
        title = mr.title[:55] + '…' if len(mr.title) > 55 else mr.title
        print(f"!{mr.iid:<5} {mr.project_id:<10} {author:<20} {branch:<45} {title}")
    print(f"\nTotal: {len(mrs)} open MR(s)\n")
    print("Links:")
    for mr in sorted(mrs, key=lambda m: m.project_id):
        print(f"  !{mr.iid} — {fix_url(mr.web_url)}")
