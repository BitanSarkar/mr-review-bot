"""macOS notification via osascript."""
import os
import subprocess
import logging
from urllib.parse import urlparse

log = logging.getLogger(__name__)

def _fix_url(url: str) -> str:
    """Rewrite GitLab URLs to include the correct port (server strips it from responses)."""
    if not url:
        return url
    from dotenv import load_dotenv
    load_dotenv()
    gitlab_url = os.getenv('GITLAB_URL', '').rstrip('/')
    if not gitlab_url:
        return url
    parsed = urlparse(gitlab_url)
    if parsed.port:
        return url.replace(
            f"{parsed.scheme}://gitlab.omantel.om/",
            f"{parsed.scheme}://gitlab.omantel.om:{parsed.port}/",
        )
    return url


class Notifier:
    def notify(self, title: str, url: str, summary: str, snooze: bool = False):
        url = _fix_url(url)
        """Send a macOS notification."""
        prefix = "🔁 Reminder: " if snooze else "👀 MR Needs Review: "
        short_summary = summary[:120] + "…" if len(summary) > 120 else summary
        short_title = title[:80] + "…" if len(title) > 80 else title

        # Main notification
        self._send_notification(
            title=f"{prefix}{short_title}",
            subtitle=short_summary,
            sound="Glass",
        )

        # Also open a dialog for the URL so user can click through
        # (osascript dialog gives them a chance to open the MR)
        self._send_action_dialog(short_title, url, short_summary, snooze)

    def _send_notification(self, title: str, subtitle: str, sound: str = "Glass"):
        """Send a banner notification."""
        script = f'''
        display notification "{self._esc(subtitle)}" ¬
            with title "{self._esc(title)}" ¬
            sound name "{sound}"
        '''
        self._run(script)

    def _send_action_dialog(self, title: str, url: str, summary: str, snooze: bool):
        """Show a dialog with Open MR / Dismiss buttons."""
        snooze_note = "\n\n(Will remind again in 2 minutes if not dismissed)" if not snooze else "\n\n(Repeated reminder)"
        message = f"{summary}{snooze_note}"
        script = f'''
        set result to button returned of (display dialog "{self._esc(message)}" ¬
            with title "MR Review: {self._esc(title)}" ¬
            buttons {{"Dismiss", "Open MR"}} ¬
            default button "Open MR" ¬
            giving up after 30)
        if result is "Open MR" then
            open location "{url}"
        end if
        '''
        # Run non-blocking so bot doesn't hang waiting for user input
        try:
            subprocess.Popen(
                ['osascript', '-e', script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log.error(f"Failed to show action dialog: {e}")

    def _run(self, script: str):
        try:
            subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            log.warning("osascript timed out")
        except Exception as e:
            log.error(f"osascript error: {e}")

    @staticmethod
    def _esc(text: str) -> str:
        """Escape text for AppleScript string literals."""
        return text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
