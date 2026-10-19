"""
AI-Relay Grok CLI Adapter
Adapter for xAI's Grok CLI.
"""

import os
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

from providers.base import ProviderAdapter, ProviderStatus, QuotaStatus, SessionState


class GrokAdapter(ProviderAdapter):
    """Adapter for Grok CLI."""

    name = "grok"
    display_name = "Grok CLI"
    cli_command = "grok"
    max_context = 128000
    priority = 3

    rate_limit_patterns = [
        r"rate.?limit",
        r"too many requests",
        r"quota exceeded",
        r"429",
        r"usage limit",
        r"please try again",
    ]

    def __init__(self):
        super().__init__()
        self._session_dir = Path.home() / ".grok"
        self._captured_output = []

    # =========================================================================
    # Status Checks
    # =========================================================================

    def check_auth(self) -> bool:
        """Check if Grok CLI is authenticated."""
        # Check for config or API key
        config_file = self._session_dir / "config.json"
        if config_file.exists():
            try:
                with open(config_file) as f:
                    config = json.load(f)
                    return bool(config.get("api_key") or config.get("auth"))
            except:
                pass

        # Check environment variable
        if os.environ.get("XAI_API_KEY"):
            return True

        # Try running a command
        stdout, stderr, code = self.run_command(["--version"], timeout=10)
        return code == 0

    def check_quota(self) -> QuotaStatus:
        """Check Grok quota status."""
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
        """Capture context from Grok session."""
        transcript = []
        file_changes = []
        working_dir = os.getcwd()
        task_state = None

        # Method 1: Use captured output from wrapper
        if self._captured_output:
            transcript = self._parse_captured_output(self._captured_output)

        # Method 2: Try to read Grok's session files
        if not transcript:
            session_file = self._find_latest_session()
            if session_file:
                transcript = self._parse_session_file(session_file)

        # Method 3: Get git changes
        file_changes = self._get_git_changes()

        # Method 4: Get task state
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
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "cli_version": self._get_version()
            }
        )

    def inject_context(self, context: str, method: str = "auto") -> bool:
        """Inject context for Grok."""
        if method == "auto":
            method = "project_file"

        if method == "project_file":
            return self._inject_via_instructions_file(context)
        elif method == "system_prompt" or method == "first_message":
            self._pending_context = context
            return True
        else:
            return False

    def _inject_via_instructions_file(self, context: str) -> bool:
        """Inject context via GROK.md or .grok/instructions.md."""
        # Try GROK.md first (similar to CLAUDE.md)
        grok_md = Path.cwd() / "GROK.md"

        relay_block = f"""
<!-- RELAY_CONTEXT_START -->
## Relayed Session Context
{context}
<!-- RELAY_CONTEXT_END -->
"""
        try:
            if grok_md.exists():
                existing = grok_md.read_text()
                import re
                existing = re.sub(
                    r'<!-- RELAY_CONTEXT_START -->.*?<!-- RELAY_CONTEXT_END -->\n?',
                    '',
                    existing,
                    flags=re.DOTALL
                )
                grok_md.write_text(existing + "\n" + relay_block)
            else:
                grok_md.write_text(relay_block)
            return True
        except Exception as e:
            print(f"Failed to inject context: {e}")
            return False

    # =========================================================================
    # Process Management
    # =========================================================================

    def launch(self, wrapped: bool = True) -> subprocess.Popen:
        """Launch Grok CLI."""
        args = self.cli_args.copy()

        # If we have pending context, inject it
        if hasattr(self, '_pending_context') and self._pending_context:
            self._inject_via_instructions_file(self._pending_context)
            self._pending_context = None

        if wrapped and sys.platform != "win32":
            return self._launch_wrapped_pty()
        else:
            return self._launch_direct()

    def _launch_direct(self) -> subprocess.Popen:
        """Launch Grok CLI directly."""
        self._process = subprocess.Popen(
            [self.cli_command] + self.cli_args,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr
        )
        return self._process

    def _launch_wrapped_pty(self) -> subprocess.Popen:
        """Launch Grok CLI with PTY wrapper."""
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
        """Get Grok CLI version."""
        stdout, _, _ = self.run_command(["--version"])
        return stdout.strip() if stdout else "unknown"

    def _find_latest_session(self) -> Optional[Path]:
        """Find the most recent Grok session file."""
        if not self._session_dir.exists():
            return None

        sessions = list(self._session_dir.rglob("*.jsonl"))
        if not sessions:
            sessions = list(self._session_dir.rglob("*.json"))
        if not sessions:
            return None

        return max(sessions, key=lambda p: p.stat().st_mtime)

    def _parse_session_file(self, path: Path) -> List[Dict[str, str]]:
        """Parse Grok session file into transcript."""
        transcript = []
        try:
            with open(path) as f:
                if path.suffix == ".jsonl":
                    for line in f:
                        try:
                            entry = json.loads(line)
                            role = entry.get("role", "")
                            content = entry.get("content", "")
                            if role and content:
                                transcript.append({"role": role, "content": content})
                        except json.JSONDecodeError:
                            continue
                else:
                    data = json.load(f)
                    if isinstance(data, list):
                        for entry in data:
                            if isinstance(entry, dict):
                                role = entry.get("role", "")
                                content = entry.get("content", "")
                                if role and content:
                                    transcript.append({"role": role, "content": content})
        except Exception:
            pass
        return transcript

    def _parse_captured_output(self, output_chunks: List[str]) -> List[Dict[str, str]]:
        """Parse captured I/O into transcript."""
        full_output = "".join(output_chunks)
        transcript = []
        parts = full_output.split("\n> ")
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
        task_files = ["TODO.md", "TASKS.md", ".tasks.json"]
        for filename in task_files:
            path = Path.cwd() / filename
            if path.exists():
                try:
                    content = path.read_text()
                    if filename.endswith(".json"):
                        return json.loads(content)
                    else:
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
