"""
AI-Relay Provider Adapter Base Class
Abstract base for all LLM CLI provider adapters.
"""

import os
import subprocess
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from enum import Enum


class ProviderStatus(Enum):
    AVAILABLE = "available"
    RATE_LIMITED = "rate_limited"
    NOT_INSTALLED = "not_installed"
    NOT_AUTHENTICATED = "not_authenticated"
    ERROR = "error"


@dataclass
class QuotaStatus:
    """Quota information for a provider."""
    provider: str
    used: int
    limit: Optional[int]
    remaining: Optional[int]
    reset_at: Optional[str]
    status: ProviderStatus

    @property
    def available(self) -> bool:
        if self.status != ProviderStatus.AVAILABLE:
            return False
        if self.limit is None:
            return True
        return self.used < self.limit

    @property
    def percentage_used(self) -> float:
        if self.limit is None or self.limit == 0:
            return 0.0
        return (self.used / self.limit) * 100


@dataclass
class SessionState:
    """Captured state from a provider session."""
    provider: str
    transcript: List[Dict[str, str]]  # [{"role": "user/assistant", "content": "..."}]
    file_changes: List[Dict[str, str]]  # [{"path": "...", "action": "modified/created"}]
    working_directory: str
    task_state: Optional[Dict[str, Any]]
    metadata: Dict[str, Any]


class ProviderAdapter(ABC):
    """
    Abstract base class for LLM CLI provider adapters.

    Each adapter knows how to:
    - Check if the CLI is installed
    - Check authentication status
    - Check quota/rate limit status
    - Capture session context
    - Inject context into a new session
    - Launch the CLI (wrapped or direct)
    """

    # Override in subclasses
    name: str = "base"
    display_name: str = "Base Provider"
    cli_command: str = ""
    cli_args: List[str] = []
    max_context: int = 100000
    priority: int = 10  # Lower = higher priority

    # Rate limit detection patterns (regex)
    rate_limit_patterns: List[str] = []

    # Session file locations (for capturing context)
    session_paths: List[str] = []

    def __init__(self):
        self._process = None

    # =========================================================================
    # Status Checks
    # =========================================================================

    def is_installed(self) -> bool:
        """Check if the CLI is installed and accessible."""
        if not self.cli_command:
            return False
        return shutil.which(self.cli_command) is not None

    @abstractmethod
    def check_auth(self) -> bool:
        """Check if the user is authenticated with this provider."""
        pass

    @abstractmethod
    def check_quota(self) -> QuotaStatus:
        """Check current quota/rate limit status."""
        pass

    def get_status(self) -> ProviderStatus:
        """Get overall provider status."""
        if not self.is_installed():
            return ProviderStatus.NOT_INSTALLED
        if not self.check_auth():
            return ProviderStatus.NOT_AUTHENTICATED
        quota = self.check_quota()
        return quota.status

    # =========================================================================
    # Context Operations
    # =========================================================================

    @abstractmethod
    def capture_session(self) -> Optional[SessionState]:
        """
        Capture current session state from the provider.

        This may read from:
        - Session files on disk
        - Clipboard (user paste)
        - Running process stdout capture
        """
        pass

    @abstractmethod
    def inject_context(self, context: str, method: str = "auto") -> bool:
        """
        Inject context into the provider for the next session.

        Methods:
        - "system_prompt": Inject as system prompt (if supported)
        - "project_file": Write to CLAUDE.md or equivalent
        - "first_message": Send as first user message
        - "auto": Choose best method for this provider
        """
        pass

    def format_context_for_injection(self, state: SessionState,
                                      compression_level: str = "standard") -> str:
        """Format captured state into injectable context."""
        lines = [
            f"# Session Context (Relayed from {state.provider})",
            "",
            "## Summary",
            f"Working directory: {state.working_directory}",
            "",
        ]

        # Add file changes
        if state.file_changes:
            lines.append("## Modified Files")
            for change in state.file_changes[:10]:  # Limit to 10
                lines.append(f"- {change['path']} ({change.get('action', 'modified')})")
            lines.append("")

        # Add task state
        if state.task_state:
            lines.append("## Current Tasks")
            if isinstance(state.task_state, dict):
                for key, value in state.task_state.items():
                    lines.append(f"- {key}: {value}")
            lines.append("")

        # Add conversation summary (compressed)
        if state.transcript:
            lines.append("## Recent Conversation")
            # For standard compression, keep last 10 messages
            recent = state.transcript[-10:] if compression_level == "standard" else state.transcript[-5:]
            for msg in recent:
                role = msg.get("role", "unknown").title()
                content = msg.get("content", "")
                # Truncate long messages
                if len(content) > 500 and compression_level != "full":
                    content = content[:500] + "..."
                lines.append(f"**{role}:** {content}")
                lines.append("")

        lines.append("---")
        lines.append("Continue from here.")

        return "\n".join(lines)

    # =========================================================================
    # Process Management
    # =========================================================================

    @abstractmethod
    def launch(self, wrapped: bool = True) -> subprocess.Popen:
        """
        Launch the CLI.

        Args:
            wrapped: If True, launch through AI-Relay wrapper for I/O capture.
                    If False, launch directly (for manual mode).

        Returns:
            Subprocess handle
        """
        pass

    def terminate(self):
        """Terminate any running process."""
        if self._process and self._process.poll() is None:
            self._process.terminate()
            self._process.wait(timeout=5)
            self._process = None

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def run_command(self, args: List[str], timeout: int = 30) -> tuple:
        """Run a CLI command and return (stdout, stderr, returncode)."""
        try:
            result = subprocess.run(
                [self.cli_command] + args,
                capture_output=True,
                text=True,
                timeout=timeout
            )
            return result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            return "", "Command timed out", -1
        except FileNotFoundError:
            return "", f"{self.cli_command} not found", -1

    def detect_rate_limit(self, output: str) -> bool:
        """Check if output indicates a rate limit."""
        import re
        output_lower = output.lower()
        for pattern in self.rate_limit_patterns:
            if re.search(pattern, output_lower):
                return True
        return False

    def estimate_tokens(self, text: str) -> int:
        """Rough token estimate (4 chars per token average)."""
        return len(text) // 4

    def __repr__(self):
        return f"<{self.__class__.__name__} name={self.name} installed={self.is_installed()}>"
