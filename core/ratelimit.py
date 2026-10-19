"""
AI-Relay Rate Limit Detection
Monitors provider output for rate limit indicators and triggers handoffs.
"""

import re
import time
from typing import Optional, List, Callable, Dict, Any
from dataclasses import dataclass
from datetime import datetime, timezone

from providers.base import ProviderAdapter, ProviderStatus


@dataclass
class RateLimitEvent:
    """Represents a detected rate limit event."""
    provider: str
    detected_at: str
    pattern_matched: str
    output_snippet: str
    suggested_wait: Optional[int]  # seconds


# Common rate limit patterns across providers
RATE_LIMIT_PATTERNS = {
    "generic": [
        r"rate.?limit",
        r"too many requests",
        r"quota.{0,20}exceeded",  # Matches "quota exceeded" or "quota has been exceeded"
        r"exceeded.{0,20}quota",  # Also catch reversed word order
        r"usage limit",
        r"please try again",
        r"429",
        r"resource exhausted",
        r"capacity.{0,10}reached",
        r"throttl",
        r"slow down",
    ],
    "claude": [
        r"rate limit exceeded",
        r"too many messages",
        r"please wait",
        r"context limit",
        r"conversation too long",
    ],
    "openai": [
        r"rate_limit_exceeded",
        r"insufficient_quota",
        r"billing",
        r"TPM",  # Tokens per minute
        r"RPM",  # Requests per minute
    ],
    "grok": [
        r"rate limit",
        r"please try again later",
        r"usage exceeded",
    ],
    "gemini": [
        r"resource_exhausted",
        r"quota exceeded",
        r"rate limit",
    ],
}

# Patterns that suggest how long to wait
WAIT_PATTERNS = [
    (r"try again in (\d+)\s*second", lambda m: int(m.group(1))),
    (r"try again in (\d+)\s*minute", lambda m: int(m.group(1)) * 60),
    (r"wait (\d+)\s*second", lambda m: int(m.group(1))),
    (r"wait (\d+)\s*minute", lambda m: int(m.group(1)) * 60),
    (r"retry.?after[:\s]+(\d+)", lambda m: int(m.group(1))),
]


class RateLimitDetector:
    """
    Monitors output streams for rate limit indicators.

    Usage:
        detector = RateLimitDetector(on_rate_limit=handle_limit)
        detector.check_output(output_text, provider_name)
    """

    def __init__(self, on_rate_limit: Optional[Callable[[RateLimitEvent], None]] = None):
        """
        Initialize detector.

        Args:
            on_rate_limit: Callback function when rate limit is detected
        """
        self.on_rate_limit = on_rate_limit
        self._compiled_patterns: Dict[str, List[re.Pattern]] = {}
        self._compile_patterns()
        self._recent_events: List[RateLimitEvent] = []
        self._cooldown_until: float = 0

    def _compile_patterns(self):
        """Compile all regex patterns for efficiency."""
        for provider, patterns in RATE_LIMIT_PATTERNS.items():
            self._compiled_patterns[provider] = [
                re.compile(p, re.IGNORECASE) for p in patterns
            ]

    def check_output(self, output: str, provider: str = "generic") -> Optional[RateLimitEvent]:
        """
        Check output text for rate limit indicators.

        Args:
            output: Output text to check
            provider: Provider name for provider-specific patterns

        Returns:
            RateLimitEvent if rate limit detected, None otherwise
        """
        if not output:
            return None

        # Cooldown to prevent duplicate events
        if time.time() < self._cooldown_until:
            return None

        # Check provider-specific patterns first
        patterns_to_check = self._compiled_patterns.get(provider, [])
        patterns_to_check.extend(self._compiled_patterns.get("generic", []))

        for pattern in patterns_to_check:
            match = pattern.search(output)
            if match:
                # Extract wait time if available
                suggested_wait = self._extract_wait_time(output)

                # Create event
                event = RateLimitEvent(
                    provider=provider,
                    detected_at=datetime.now(timezone.utc).isoformat(),
                    pattern_matched=pattern.pattern,
                    output_snippet=output[:500] if len(output) > 500 else output,
                    suggested_wait=suggested_wait
                )

                self._recent_events.append(event)

                # Set cooldown (5 seconds by default, or suggested wait)
                self._cooldown_until = time.time() + (suggested_wait or 5)

                # Trigger callback
                if self.on_rate_limit:
                    self.on_rate_limit(event)

                return event

        return None

    def _extract_wait_time(self, output: str) -> Optional[int]:
        """Extract suggested wait time from output."""
        output_lower = output.lower()
        for pattern, extractor in WAIT_PATTERNS:
            match = re.search(pattern, output_lower)
            if match:
                try:
                    return extractor(match)
                except:
                    pass
        return None

    def get_recent_events(self, limit: int = 10) -> List[RateLimitEvent]:
        """Get recent rate limit events."""
        return self._recent_events[-limit:]

    def clear_events(self):
        """Clear event history."""
        self._recent_events.clear()


class AutoHandoffManager:
    """
    Manages automatic handoffs when rate limits are detected.

    Usage:
        manager = AutoHandoffManager(providers=[claude, codex, grok])
        manager.start_monitoring(current_provider)
    """

    def __init__(self,
                 providers: List[ProviderAdapter],
                 on_handoff: Optional[Callable[[str, str, str], None]] = None):
        """
        Initialize manager.

        Args:
            providers: List of available provider adapters
            on_handoff: Callback(from_provider, to_provider, context) when handoff occurs
        """
        self.providers = {p.name: p for p in providers}
        self.on_handoff = on_handoff
        self.detector = RateLimitDetector(on_rate_limit=self._handle_rate_limit)
        self._current_provider: Optional[str] = None
        self._session_context: str = ""
        self._handoff_count: int = 0
        self._max_handoffs: int = 5  # Prevent infinite loops

    def set_current_provider(self, provider_name: str):
        """Set the currently active provider."""
        self._current_provider = provider_name

    def set_context(self, context: str):
        """Set the current session context for handoff."""
        self._session_context = context

    def check_output(self, output: str) -> bool:
        """
        Check output and trigger handoff if needed.

        Returns:
            True if handoff was triggered
        """
        if not self._current_provider:
            return False

        event = self.detector.check_output(output, self._current_provider)
        return event is not None

    def _handle_rate_limit(self, event: RateLimitEvent):
        """Handle a rate limit detection."""
        if self._handoff_count >= self._max_handoffs:
            print(f"[AI-Relay] Max handoffs ({self._max_handoffs}) reached, not switching")
            return

        # Find next available provider
        next_provider = self._get_next_provider(event.provider)
        if not next_provider:
            print(f"[AI-Relay] No alternative providers available")
            return

        self._handoff_count += 1

        print(f"\n[AI-Relay] Rate limit detected on {event.provider}")
        print(f"[AI-Relay] Switching to {next_provider}...")

        if self.on_handoff:
            self.on_handoff(event.provider, next_provider, self._session_context)

        self._current_provider = next_provider

    def _get_next_provider(self, current: str) -> Optional[str]:
        """Get the next available provider."""
        # Sort by priority
        sorted_providers = sorted(
            self.providers.values(),
            key=lambda p: p.priority
        )

        for provider in sorted_providers:
            if provider.name == current:
                continue
            if not provider.is_installed():
                continue
            status = provider.get_status()
            if status == ProviderStatus.AVAILABLE:
                return provider.name

        return None

    def reset_handoff_count(self):
        """Reset handoff counter (call at session start)."""
        self._handoff_count = 0


def create_rate_limit_handler(db, session_id: str) -> Callable[[RateLimitEvent], None]:
    """
    Create a rate limit handler that logs to the database.

    Args:
        db: RelayDatabase instance
        session_id: Current session ID

    Returns:
        Handler function
    """
    def handler(event: RateLimitEvent):
        # Update provider quota
        quota = db.get_quota(event.provider)
        if quota:
            # Mark as rate limited
            db.update_quota(event.provider, is_available=False)

        # Log the event
        print(f"[AI-Relay] Rate limit logged for {event.provider}")

    return handler
