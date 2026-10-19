"""
AI-Relay Claude Code Adapter
Adapter for Anthropic's Claude Code CLI.
"""

import os
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime

# PTY support is Unix-only
if sys.platform != "win32":
    import pty
    import select

from providers.base import ProviderAdapter, ProviderStatus, QuotaStatus, SessionState


class ClaudeAdapter(ProviderAdapter):
    """Adapter for Claude Code CLI."""

    name = "claude"
    display_name = "Claude Code"
    cli_command = "claude"
    max_context = 200000
    priority = 1  # Highest priority

    rate_limit_patterns = [
        r"rate.?limit",
        r"too many requests",
        r"quota exceeded",
        r"please try again later",
        r"usage limit",
    ]

    def __init__(self):
        super().__init__()
        self._session_dir = Path.home() / ".claude"
        self._captured_output = []

    # =========================================================================
    # Status Checks
    # =========================================================================

    def check_auth(self) -> bool:
        """Check if Claude CLI is authenticated."""
        stdout, stderr, code = self.run_command(["--version"], timeout=10)
        if code != 0:
            return False

        # Try a simple status check
        # Claude CLI doesn't have a direct "am I logged in" command,
        # but we can check if config exists
        config_file = self._session_dir / "config.json"
        if config_file.exists():
            try:
                with open(config_file) as f:
                    config = json.load(f)
                    # Check for auth token or session
                    return bool(config.get("auth") or config.get("session"))
            except:
                pass

        # Fallback: assume authenticated if CLI runs
        return code == 0

    def check_quota(self) -> QuotaStatus:
        """Check Claude quota status."""
        # Claude doesn't expose quota via CLI easily
        # We track usage ourselves in the database
        from core.database import get_db

        db = get_db()
        quota = db.get_quota(self.name)

        if quota:
            remaining = None
            if quota.daily_limit:
                remaining = max(0, quota.daily_limit - quota.used_today)

            status = ProviderStatus.AVAILABLE
            if quota.daily_limit and quota.used_today >= quota.daily_limit:
                status = ProviderStatus.RATE_LIMITED
            if not quota.is_available:
                status = ProviderStatus.ERROR

            return QuotaStatus(
                provider=self.name,
                used=quota.used_today,
                limit=quota.daily_limit,
                remaining=remaining,
                reset_at=quota.reset_at,
                status=status
            )

        # No quota info - assume available with no limit
        return QuotaStatus(
            provider=self.name,
            used=0,
            limit=None,
            remaining=None,
            reset_at=None,
            status=ProviderStatus.AVAILABLE if self.is_installed() else ProviderStatus.NOT_INSTALLED
        )

    # =========================================================================
    # Context Operations
    # =========================================================================

    def capture_session(self) -> Optional[SessionState]:
        """
        Capture context from Claude session.

        Tries multiple methods:
        1. Read from captured I/O (if wrapped)
        2. Read from Claude's session files
        3. Read from project's CLAUDE.md
        """
        transcript = []
        file_changes = []
        working_dir = os.getcwd()
        task_state = None

        # Method 1: Use captured output from wrapper
        if self._captured_output:
            transcript = self._parse_captured_output(self._captured_output)

        # Method 2: Try to read Claude's session files
        if not transcript:
            session_file = self._find_latest_session()
            if session_file:
                transcript = self._parse_session_file(session_file)

        # Method 3: Try to get git diff for file changes
        file_changes = self._get_git_changes()

        # Method 4: Try to read task state from any todo list
        task_state = self._get_task_state()

        if not transcript and not file_changes:
            return None

        return SessionState(
            provider=self.display_name,
            transcript=transcript,
            file_changes=file_changes,
            working_directory=working_dir,
            task_state=task_state,
            metadata={
                "captured_at": datetime.utcnow().isoformat(),
                "cli_version": self._get_version()
            }
        )

    def inject_context(self, context: str, method: str = "auto") -> bool:
        """Inject context for Claude."""
        if method == "auto":
            method = "project_file"  # CLAUDE.md is best for Claude

        if method == "project_file":
            return self._inject_via_claude_md(context)
        elif method == "first_message":
            # Store for later sending
            self._pending_context = context
            return True
        else:
            return False

    def _inject_via_claude_md(self, context: str) -> bool:
        """Inject context by appending to CLAUDE.md."""
        claude_md = Path.cwd() / "CLAUDE.md"

        # Prepare relay context block
        relay_block = f"""
<!-- RELAY_CONTEXT_START -->
## Relayed Session Context
{context}
<!-- RELAY_CONTEXT_END -->
"""
        try:
            if claude_md.exists():
                existing = claude_md.read_text()
                # Remove old relay context if present
                import re
                existing = re.sub(
                    r'<!-- RELAY_CONTEXT_START -->.*?<!-- RELAY_CONTEXT_END -->\n?',
                    '',
                    existing,
                    flags=re.DOTALL
                )
                claude_md.write_text(existing + "\n" + relay_block)
            else:
                claude_md.write_text(relay_block)
            return True
        except Exception as e:
            print(f"Failed to inject context: {e}")
            return False

    # =========================================================================
    # Process Management
    # =========================================================================

    def launch(self, wrapped: bool = True) -> subprocess.Popen:
        """Launch Claude CLI."""
        if wrapped and sys.platform != "win32":
            return self._launch_wrapped_pty()
        else:
            return self._launch_direct()

    def _launch_direct(self) -> subprocess.Popen:
        """Launch Claude CLI directly."""
        self._process = subprocess.Popen(
            [self.cli_command] + self.cli_args,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr
        )
        return self._process

    def _launch_wrapped_pty(self) -> subprocess.Popen:
        """Launch Claude CLI with PTY wrapper for I/O capture (Unix only)."""
        import pty
        import os

        master_fd, slave_fd = pty.openpty()

        self._process = subprocess.Popen(
            [self.cli_command] + self.cli_args,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True
        )

        os.close(slave_fd)
        self._master_fd = master_fd
        self._captured_output = []

        return self._process

    def read_output(self, timeout: float = 0.1) -> Optional[str]:
        """Read output from wrapped process."""
        if not hasattr(self, '_master_fd'):
            return None

        import select
        ready, _, _ = select.select([self._master_fd], [], [], timeout)
        if ready:
            try:
                data = os.read(self._master_fd, 4096).decode('utf-8', errors='replace')
                self._captured_output.append(data)
                return data
            except OSError:
                return None
        return None

    def write_input(self, data: str):
        """Write input to wrapped process."""
        if hasattr(self, '_master_fd'):
            os.write(self._master_fd, data.encode('utf-8'))

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_version(self) -> str:
        """Get Claude CLI version."""
        stdout, _, _ = self.run_command(["--version"])
        return stdout.strip() if stdout else "unknown"

    def _find_latest_session(self) -> Optional[Path]:
        """Find the most recent Claude session file."""
        # Claude stores sessions in ~/.claude/projects/
        projects_dir = self._session_dir / "projects"
        if not projects_dir.exists():
            return None

        # Find most recent .jsonl file
        sessions = list(projects_dir.rglob("*.jsonl"))
        if not sessions:
            return None

        return max(sessions, key=lambda p: p.stat().st_mtime)

    def _parse_session_file(self, path: Path) -> List[Dict[str, str]]:
        """Parse Claude session file into transcript."""
        transcript = []
        try:
            with open(path) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "user":
                            transcript.append({
                                "role": "user",
                                "content": entry.get("content", "")
                            })
                        elif entry.get("type") == "assistant":
                            transcript.append({
                                "role": "assistant",
                                "content": entry.get("content", "")
                            })
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass
        return transcript

    def _parse_captured_output(self, output_chunks: List[str]) -> List[Dict[str, str]]:
        """Parse captured I/O into transcript."""
        # Simple heuristic: alternate user/assistant based on patterns
        # This is a basic implementation - real parsing would be more sophisticated
        full_output = "".join(output_chunks)
        transcript = []

        # Split on common patterns
        parts = full_output.split("\n> ")  # User prompt indicator
        for i, part in enumerate(parts):
            if not part.strip():
                continue
            role = "user" if i % 2 == 1 else "assistant"
            transcript.append({"role": role, "content": part.strip()})

        return transcript

    def _get_git_changes(self) -> List[Dict[str, str]]:
        """Get list of changed files from git."""
        changes = []
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if not line:
                        continue
                    status = line[:2].strip()
                    path = line[3:]
                    action = "modified"
                    if "A" in status:
                        action = "created"
                    elif "D" in status:
                        action = "deleted"
                    elif "?" in status:
                        action = "untracked"
                    changes.append({"path": path, "action": action})
        except:
            pass
        return changes

    def _get_task_state(self) -> Optional[Dict[str, Any]]:
        """Try to extract task state from project."""
        # Check for common task file patterns
        task_files = ["TODO.md", "TASKS.md", ".tasks.json"]
        for filename in task_files:
            path = Path.cwd() / filename
            if path.exists():
                try:
                    content = path.read_text()
                    if filename.endswith(".json"):
                        return json.loads(content)
                    else:
                        # Parse markdown todo items
                        tasks = []
                        for line in content.split("\n"):
                            if line.strip().startswith("- [ ]"):
                                tasks.append({"task": line[5:].strip(), "done": False})
                            elif line.strip().startswith("- [x]"):
                                tasks.append({"task": line[5:].strip(), "done": True})
                        if tasks:
                            return {"tasks": tasks}
                except:
                    pass
        return None
