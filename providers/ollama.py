"""
AI-Relay Ollama Adapter
Adapter for local Ollama LLM server - always available fallback.
"""

import os
import json
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

from providers.base import ProviderAdapter, ProviderStatus, QuotaStatus, SessionState


class OllamaAdapter(ProviderAdapter):
    """Adapter for local Ollama server.

    Ollama is a local LLM server that can run various models.
    It serves as an always-available fallback when cloud providers
    are rate-limited or unavailable.
    """

    name = "ollama"
    display_name = "Ollama (Local)"
    cli_command = "ollama"
    max_context = 128000  # Depends on model
    priority = 99  # Lowest priority - fallback

    rate_limit_patterns = []  # Local server doesn't rate limit

    def __init__(self, model: str = "llama3.1:8b", base_url: str = "http://localhost:11434"):
        super().__init__()
        self.model = model
        self.base_url = base_url
        self._session_dir = Path.home() / ".ollama"
        self._captured_output = []

    # =========================================================================
    # Status Checks
    # =========================================================================

    def is_installed(self) -> bool:
        """Check if Ollama server is running."""
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=2) as response:
                return response.status == 200
        except:
            # Fall back to checking CLI
            return super().is_installed()

    def check_auth(self) -> bool:
        """Ollama doesn't require authentication."""
        return self.is_installed()

    def check_quota(self) -> QuotaStatus:
        """Ollama has no quota - it's local."""
        return QuotaStatus(
            provider=self.name,
            used=0,
            limit=None,  # No limit for local
            remaining=None,
            reset_at=None,
            status=ProviderStatus.AVAILABLE if self.is_installed() else ProviderStatus.NOT_INSTALLED
        )

    def get_available_models(self) -> List[str]:
        """Get list of models available on the Ollama server."""
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                return [model['name'] for model in data.get('models', [])]
        except:
            return []

    # =========================================================================
    # Context Operations
    # =========================================================================

    def capture_session(self) -> Optional[SessionState]:
        """Capture context from Ollama session."""
        transcript = []
        file_changes = []
        working_dir = os.getcwd()
        task_state = None

        # Method 1: Use captured output from wrapper
        if self._captured_output:
            transcript = self._parse_captured_output(self._captured_output)

        # Method 2: Get git changes
        file_changes = self._get_git_changes()

        # Method 3: Get task state
        task_state = self._get_task_state()

        if not transcript and not file_changes:
            return None

        return SessionState(
            provider=f"{self.display_name} ({self.model})",
            transcript=transcript,
            file_changes=file_changes,
            working_directory=working_dir,
            task_state=task_state,
            metadata={
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "model": self.model,
                "base_url": self.base_url
            }
        )

    def inject_context(self, context: str, method: str = "auto") -> bool:
        """Inject context for Ollama - use system prompt."""
        if method == "auto":
            method = "system_prompt"

        if method == "system_prompt" or method == "first_message":
            self._pending_context = context
            return True
        elif method == "project_file":
            return self._inject_via_instructions_file(context)
        else:
            return False

    def _inject_via_instructions_file(self, context: str) -> bool:
        """Inject context via .ollama/instructions.md."""
        ollama_dir = Path.cwd() / ".ollama"
        ollama_dir.mkdir(exist_ok=True)
        instructions_file = ollama_dir / "instructions.md"

        relay_block = f"""
<!-- RELAY_CONTEXT_START -->
## Relayed Session Context
{context}
<!-- RELAY_CONTEXT_END -->
"""
        try:
            if instructions_file.exists():
                existing = instructions_file.read_text()
                import re
                existing = re.sub(
                    r'<!-- RELAY_CONTEXT_START -->.*?<!-- RELAY_CONTEXT_END -->\n?',
                    '',
                    existing,
                    flags=re.DOTALL
                )
                instructions_file.write_text(existing + "\n" + relay_block)
            else:
                instructions_file.write_text(relay_block)
            return True
        except Exception as e:
            print(f"Failed to inject context: {e}")
            return False

    # =========================================================================
    # Chat API
    # =========================================================================

    def chat(self, messages: List[Dict[str, str]], stream: bool = True) -> str:
        """Send chat request to Ollama API."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": stream
        }

        # Add pending context as system message
        if hasattr(self, '_pending_context') and self._pending_context:
            payload["messages"] = [
                {"role": "system", "content": self._pending_context}
            ] + payload["messages"]
            self._pending_context = None

        try:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                f"{self.base_url}/api/chat",
                data=data,
                headers={'Content-Type': 'application/json'}
            )

            response_text = ""
            with urllib.request.urlopen(req, timeout=300) as response:
                for line in response:
                    if line:
                        chunk = json.loads(line.decode())
                        if 'message' in chunk and 'content' in chunk['message']:
                            content = chunk['message']['content']
                            response_text += content
                            if stream:
                                print(content, end='', flush=True)

            if stream:
                print()  # Newline after streaming

            return response_text

        except urllib.error.URLError as e:
            raise RuntimeError(f"Failed to connect to Ollama: {e}")
        except Exception as e:
            raise RuntimeError(f"Ollama chat error: {e}")

    # =========================================================================
    # Process Management
    # =========================================================================

    def launch(self, wrapped: bool = True) -> subprocess.Popen:
        """Launch Ollama CLI or interactive session."""
        args = ["run", self.model]

        if wrapped and sys.platform != "win32":
            return self._launch_wrapped_pty()
        else:
            return self._launch_direct()

    def _launch_direct(self) -> subprocess.Popen:
        """Launch Ollama CLI directly."""
        self._process = subprocess.Popen(
            [self.cli_command, "run", self.model],
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr
        )
        return self._process

    def _launch_wrapped_pty(self) -> subprocess.Popen:
        """Launch Ollama CLI with PTY wrapper."""
        import pty

        master_fd, slave_fd = pty.openpty()

        self._process = subprocess.Popen(
            [self.cli_command, "run", self.model],
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
        """Get Ollama version."""
        stdout, _, _ = self.run_command(["--version"])
        return stdout.strip() if stdout else "unknown"

    def _parse_captured_output(self, output_chunks: List[str]) -> List[Dict[str, str]]:
        """Parse captured I/O into transcript."""
        full_output = "".join(output_chunks)
        transcript = []
        parts = full_output.split("\n>>> ")
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
