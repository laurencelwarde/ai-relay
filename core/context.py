"""
AI-Relay Context Management
Context capture, compression, and formatting.
"""

import json
import uuid
from datetime import datetime
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

from core.database import RelayDatabase, ContextSnapshot, get_db
from providers.base import SessionState


@dataclass
class CompressionConfig:
    """Configuration for context compression."""
    level: str  # full, standard, minimal, micro
    max_tokens: int
    preserve_last_n: int
    include_file_changes: bool
    include_task_state: bool


COMPRESSION_LEVELS = {
    "full": CompressionConfig(
        level="full",
        max_tokens=50000,
        preserve_last_n=100,
        include_file_changes=True,
        include_task_state=True
    ),
    "standard": CompressionConfig(
        level="standard",
        max_tokens=15000,
        preserve_last_n=10,
        include_file_changes=True,
        include_task_state=True
    ),
    "minimal": CompressionConfig(
        level="minimal",
        max_tokens=4000,
        preserve_last_n=5,
        include_file_changes=True,
        include_task_state=False
    ),
    "micro": CompressionConfig(
        level="micro",
        max_tokens=1000,
        preserve_last_n=2,
        include_file_changes=False,
        include_task_state=False
    ),
}


def estimate_tokens(text: str) -> int:
    """Estimate token count (rough: ~4 chars per token)."""
    return len(text) // 4


def compress_transcript(transcript: List[Dict[str, str]],
                        config: CompressionConfig) -> List[Dict[str, str]]:
    """Compress a transcript to fit within token limits."""
    if not transcript:
        return []

    # Keep the last N messages as-is
    preserved = transcript[-config.preserve_last_n:]

    # Summarize older messages if any
    older = transcript[:-config.preserve_last_n] if len(transcript) > config.preserve_last_n else []

    if not older:
        return preserved

    # Create summary of older messages
    summary_parts = []
    for msg in older:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        # Truncate long messages
        if len(content) > 200:
            content = content[:200] + "..."
        summary_parts.append(f"[{role}]: {content[:100]}")

    # Combine into single summary message
    if summary_parts:
        summary = {
            "role": "system",
            "content": f"[Earlier conversation summary - {len(older)} messages]\n" +
                      "\n".join(summary_parts[:20])  # Max 20 summaries
        }
        return [summary] + preserved

    return preserved


def compress_context(state: SessionState,
                     level: str = "standard") -> str:
    """
    Compress a session state into injectable context.

    Returns markdown-formatted context suitable for injection.
    """
    config = COMPRESSION_LEVELS.get(level, COMPRESSION_LEVELS["standard"])

    lines = [
        f"# Session Context (Relayed from {state.provider})",
        f"*Captured: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}*",
        "",
    ]

    # Working directory
    lines.extend([
        "## Environment",
        f"- Working directory: `{state.working_directory}`",
        "",
    ])

    # File changes
    if config.include_file_changes and state.file_changes:
        lines.append("## Modified Files")
        max_files = 20 if level == "full" else 10 if level == "standard" else 5
        for change in state.file_changes[:max_files]:
            action = change.get("action", "modified")
            path = change.get("path", "unknown")
            lines.append(f"- `{path}` ({action})")
        if len(state.file_changes) > max_files:
            lines.append(f"- ... and {len(state.file_changes) - max_files} more")
        lines.append("")

    # Task state
    if config.include_task_state and state.task_state:
        lines.append("## Current Tasks")
        if isinstance(state.task_state, dict):
            tasks = state.task_state.get("tasks", [])
            for task in tasks[:10]:
                status = "x" if task.get("done") else " "
                lines.append(f"- [{status}] {task.get('task', '')}")
        lines.append("")

    # Transcript
    if state.transcript:
        lines.append("## Recent Conversation")
        compressed = compress_transcript(state.transcript, config)

        for msg in compressed:
            role = msg.get("role", "unknown").title()
            content = msg.get("content", "")

            # Truncate based on level
            max_len = 2000 if level == "full" else 500 if level == "standard" else 200
            if len(content) > max_len:
                content = content[:max_len] + "..."

            lines.append(f"**{role}:**")
            lines.append(content)
            lines.append("")

    # Metadata
    if state.metadata:
        lines.extend([
            "## Metadata",
            f"- Source: {state.metadata.get('cli_version', 'unknown')}",
            f"- Captured: {state.metadata.get('captured_at', 'unknown')}",
            "",
        ])

    # Footer
    lines.extend([
        "---",
        "*Continue from where the previous session left off.*",
    ])

    result = "\n".join(lines)

    # Final token check - truncate if needed
    while estimate_tokens(result) > config.max_tokens and lines:
        # Remove from conversation section first
        for i, line in enumerate(lines):
            if line.startswith("**User:**") or line.startswith("**Assistant:**"):
                lines.pop(i)
                result = "\n".join(lines)
                break
        else:
            break

    return result


def create_snapshot(session_id: str,
                    provider: str,
                    state: SessionState,
                    compression_level: str = "standard",
                    db: Optional[RelayDatabase] = None) -> ContextSnapshot:
    """
    Create and save a context snapshot.

    Args:
        session_id: The relay session ID
        provider: Provider name (e.g., "claude")
        state: Captured session state
        compression_level: Compression level to use
        db: Database instance (creates new if None)

    Returns:
        The created ContextSnapshot
    """
    if db is None:
        db = get_db()

    # Compress the context
    compressed = compress_context(state, compression_level)

    # Create snapshot
    snapshot = ContextSnapshot(
        id=f"snap_{uuid.uuid4().hex[:12]}",
        session_id=session_id,
        provider=provider,
        captured_at=datetime.utcnow().isoformat(),
        full_transcript=json.dumps(state.transcript),
        compressed_summary=compressed,
        file_changes=json.dumps(state.file_changes),
        task_state=json.dumps(state.task_state) if state.task_state else "{}",
        compression_level=compression_level,
        token_count=estimate_tokens(compressed)
    )

    # Save to database
    db.save_snapshot(snapshot)

    return snapshot


def get_injectable_context(session_id: str,
                           compression_level: str = "standard",
                           db: Optional[RelayDatabase] = None) -> Optional[str]:
    """
    Get context ready for injection into a new provider.

    Args:
        session_id: The relay session ID
        compression_level: Desired compression level
        db: Database instance

    Returns:
        Compressed context string, or None if no snapshot exists
    """
    if db is None:
        db = get_db()

    snapshot = db.get_latest_snapshot(session_id)
    if not snapshot:
        return None

    # If requested level matches, use existing compressed
    if snapshot.compression_level == compression_level:
        return snapshot.compressed_summary

    # Re-compress from full transcript
    try:
        transcript = json.loads(snapshot.full_transcript)
        file_changes = json.loads(snapshot.file_changes)
        task_state = json.loads(snapshot.task_state) if snapshot.task_state else None

        state = SessionState(
            provider=snapshot.provider,
            transcript=transcript,
            file_changes=file_changes,
            working_directory=".",  # Not stored in snapshot
            task_state=task_state,
            metadata={}
        )

        return compress_context(state, compression_level)
    except Exception as e:
        print(f"Error re-compressing context: {e}")
        return snapshot.compressed_summary
