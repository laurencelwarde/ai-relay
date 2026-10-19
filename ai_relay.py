#!/usr/bin/env python3
"""
AI-Relay - Cross-Provider Context Continuity

Seamlessly hand off AI coding sessions between Claude, Codex, Grok, and more.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Fix Windows console encoding for UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from core.database import RelayDatabase, ProviderQuota, get_db
from core.context import (
    compress_context, create_snapshot, get_injectable_context,
    estimate_tokens, COMPRESSION_LEVELS
)
from providers.base import ProviderAdapter, ProviderStatus
from providers.claude import ClaudeAdapter
from providers.codex import CodexAdapter
from providers.grok import GrokAdapter
from providers.gemini import GeminiAdapter
from providers.ollama import OllamaAdapter


# Provider registry
PROVIDERS = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "grok": GrokAdapter,
    "gemini": GeminiAdapter,
    "ollama": OllamaAdapter,
}


def get_provider(name: str) -> Optional[ProviderAdapter]:
    """Get a provider adapter by name."""
    adapter_class = PROVIDERS.get(name.lower())
    if adapter_class:
        return adapter_class()
    return None


def list_providers() -> dict:
    """List all available providers with their status."""
    result = {}
    for name, adapter_class in PROVIDERS.items():
        adapter = adapter_class()
        result[name] = {
            "display_name": adapter.display_name,
            "installed": adapter.is_installed(),
            "status": adapter.get_status().value,
            "priority": adapter.priority,
        }
    return result


# =============================================================================
# CLI Commands
# =============================================================================

def cmd_status(args):
    """Show status of all providers and current session."""
    db = get_db()

    providers = list_providers()
    quotas = {q.provider: q for q in db.list_quotas()}
    session = db.get_active_session()
    stats = db.get_stats()

    # JSON output for VS Code extension
    if hasattr(args, 'json') and args.json:
        output = {
            "providers": [],
            "session": None,
            "stats": stats
        }

        for name, info in providers.items():
            quota = quotas.get(name)
            output["providers"].append({
                "name": name,
                "displayName": info['display_name'],
                "installed": info['installed'],
                "status": info['status'],
                "used": quota.used_today if quota else 0,
                "limit": quota.daily_limit if quota else None,
                "enabled": True  # Default enabled
            })

        if session:
            output["session"] = {
                "id": session.id,
                "provider": session.last_provider,
                "created": session.created_at,
                "tokens": session.total_tokens_used
            }

        print(json.dumps(output))
        return 0

    # Human-readable output
    print("\n" + "=" * 50)
    print("  AI-Relay Status")
    print("=" * 50)

    # Provider status
    print("\n📡 Providers:\n")

    for name, info in sorted(providers.items(), key=lambda x: x[1]['priority']):
        status_icon = "✓" if info['installed'] else "✗"

        quota = quotas.get(name)
        if quota and quota.daily_limit:
            bar_width = 20
            filled = int((quota.used_today / quota.daily_limit) * bar_width)
            bar = "█" * filled + "░" * (bar_width - filled)
            quota_str = f"[{bar}] {quota.used_today}/{quota.daily_limit}"
        else:
            quota_str = "[No limit]"

        print(f"  {status_icon} {info['display_name']:15} {quota_str:30} ({info['status']})")

    # Current session
    print("\n📋 Session:\n")
    if session:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(session.created_at)
        hours = age.total_seconds() / 3600
        print(f"  ID:       {session.id}")
        print(f"  Project:  {session.project_path}")
        print(f"  Provider: {session.last_provider}")
        print(f"  Active:   {hours:.1f} hours")
        print(f"  Tokens:   {session.total_tokens_used:,}")

        # Get latest snapshot
        snapshot = db.get_latest_snapshot(session.id)
        if snapshot:
            print(f"\n  Latest snapshot: {snapshot.token_count:,} tokens ({snapshot.compression_level})")
    else:
        print("  No active session")

    # Stats
    print("\n📊 Statistics:\n")
    print(f"  Total sessions:  {stats['total_sessions']}")
    print(f"  Total handoffs:  {stats['total_handoffs']}")
    print(f"  Total tokens:    {stats['total_tokens']:,}")

    print("\n" + "=" * 50 + "\n")

    return 0


def cmd_capture(args):
    """Capture context from current or specified provider."""
    db = get_db()

    # Determine provider
    provider_name = args.provider
    if not provider_name:
        # Try to detect from active session
        session = db.get_active_session()
        if session:
            provider_name = session.last_provider
        else:
            # Default to claude
            provider_name = "claude"

    provider = get_provider(provider_name)
    if not provider:
        print(f"❌ Unknown provider: {provider_name}")
        print(f"   Available: {', '.join(PROVIDERS.keys())}")
        return 1

    print(f"[AI-Relay] Capturing context from {provider.display_name}...")

    # Capture session state
    state = provider.capture_session()
    if not state:
        print("❌ No session data found to capture")
        print("   Try running inside a project directory with git changes,")
        print("   or paste your conversation manually with --clipboard")
        return 1

    # Get or create session
    session = db.get_active_session(os.getcwd())
    if not session:
        session = db.create_session(os.getcwd(), provider_name)
        print(f"[AI-Relay] Created new session: {session.id}")

    # Compress and save
    compression = args.compression or "standard"
    print(f"[AI-Relay] Compressing to {compression} level...")

    snapshot = create_snapshot(
        session_id=session.id,
        provider=provider_name,
        state=state,
        compression_level=compression,
        db=db
    )

    print(f"[AI-Relay] ✓ Captured {snapshot.token_count:,} tokens")
    print(f"[AI-Relay] Snapshot ID: {snapshot.id}")

    # Update session
    db.update_session(session.id, last_provider=provider_name,
                      total_tokens_used=session.total_tokens_used + snapshot.token_count)

    if args.show:
        print("\n" + "-" * 40)
        print(snapshot.compressed_summary)
        print("-" * 40)

    return 0


def cmd_inject(args):
    """Inject context into a provider and optionally launch it."""
    db = get_db()

    # Get target provider
    provider_name = args.provider
    if not provider_name:
        # Pick best available
        provider_name = db.get_best_available_provider()
        if not provider_name:
            # Fallback to first installed
            for name, info in list_providers().items():
                if info['installed']:
                    provider_name = name
                    break

    if not provider_name:
        print("❌ No providers available")
        return 1

    provider = get_provider(provider_name)
    if not provider:
        print(f"❌ Unknown provider: {provider_name}")
        return 1

    if not provider.is_installed():
        print(f"❌ {provider.display_name} is not installed")
        return 1

    # Get context to inject
    session_id = args.session
    if not session_id:
        session = db.get_active_session()
        if session:
            session_id = session.id

    if not session_id:
        print("❌ No session to inject from")
        print("   Run 'ai-relay capture' first, or specify --session")
        return 1

    compression = args.compression or "standard"
    context = get_injectable_context(session_id, compression, db)

    if not context:
        print("❌ No context snapshot found for session")
        return 1

    print(f"[AI-Relay] Injecting {estimate_tokens(context):,} tokens into {provider.display_name}...")

    # Inject context
    method = args.method or "auto"
    success = provider.inject_context(context, method)

    if not success:
        print("❌ Failed to inject context")
        return 1

    print(f"[AI-Relay] ✓ Context injected via {method}")

    # Log the handoff
    session = db.get_session(session_id)
    if session and session.last_provider != provider_name:
        db.log_handoff(
            session_id=session_id,
            from_provider=session.last_provider,
            to_provider=provider_name,
            context_tokens=estimate_tokens(context),
            reason="manual"
        )
        db.update_session(session_id, last_provider=provider_name)
        print(f"[AI-Relay] Handoff logged: {session.last_provider} → {provider_name}")

    # Launch if requested
    if args.launch:
        print(f"[AI-Relay] Launching {provider.display_name}...")
        provider.launch(wrapped=not args.direct)
        return 0

    print(f"\n[AI-Relay] Context ready. Launch with: {provider.cli_command}")
    return 0


def cmd_handoff(args):
    """Quick handoff: capture from current provider, inject to target, launch."""
    db = get_db()

    # Get source session
    session = db.get_active_session()
    if not session:
        print("❌ No active session to handoff")
        return 1

    from_provider = session.last_provider
    to_provider = args.provider

    if not to_provider:
        # Pick best available that isn't current
        quotas = db.list_quotas()
        for q in quotas:
            if q.provider != from_provider and q.is_available:
                if q.daily_limit is None or q.used_today < q.daily_limit:
                    to_provider = q.provider
                    break

    if not to_provider:
        print("❌ No target provider available")
        return 1

    target = get_provider(to_provider)
    if not target:
        print(f"❌ Unknown provider: {to_provider}")
        return 1

    if not target.is_installed():
        print(f"❌ {target.display_name} is not installed")
        return 1

    print(f"[AI-Relay] Handoff: {from_provider} → {to_provider}")
    print()

    # Capture
    source = get_provider(from_provider)
    if source:
        state = source.capture_session()
        if state:
            compression = args.compression or "standard"
            snapshot = create_snapshot(session.id, from_provider, state, compression, db)
            print(f"[AI-Relay] ✓ Captured {snapshot.token_count:,} tokens")
        else:
            print("[AI-Relay] ⚠ No new context captured, using existing snapshot")

    # Get context
    context = get_injectable_context(session.id, args.compression or "standard", db)
    if not context:
        print("❌ No context available to inject")
        return 1

    # Inject
    target.inject_context(context, "auto")
    print(f"[AI-Relay] ✓ Context injected into {target.display_name}")

    # Log handoff
    db.log_handoff(
        session_id=session.id,
        from_provider=from_provider,
        to_provider=to_provider,
        context_tokens=estimate_tokens(context),
        reason=args.reason or "manual"
    )
    db.update_session(session.id, last_provider=to_provider)

    # Launch
    if not args.no_launch:
        print(f"[AI-Relay] Launching {target.display_name}...")
        print()
        target.launch(wrapped=True)

    return 0


def cmd_start(args):
    """Start a new relay session with the best available provider."""
    db = get_db()

    # Pick provider
    provider_name = args.provider
    if not provider_name:
        provider_name = db.get_best_available_provider()
        if not provider_name:
            # Fallback to first installed
            for name, info in list_providers().items():
                if info['installed']:
                    provider_name = name
                    break

    if not provider_name:
        print("❌ No providers available")
        return 1

    provider = get_provider(provider_name)
    if not provider:
        print(f"❌ Unknown provider: {provider_name}")
        return 1

    if not provider.is_installed():
        print(f"❌ {provider.display_name} is not installed")
        return 1

    # Create session
    session = db.create_session(os.getcwd(), provider_name)
    print(f"[AI-Relay] Starting session: {session.id}")
    print(f"[AI-Relay] Provider: {provider.display_name}")

    # Show quota
    quota = provider.check_quota()
    if quota.limit:
        remaining = quota.limit - quota.used
        print(f"[AI-Relay] Quota: {quota.used}/{quota.limit} ({remaining} remaining)")

    print(f"[AI-Relay] Launching {provider.display_name}...")
    print()

    # Launch wrapped
    provider.launch(wrapped=True)
    return 0


def cmd_continue(args):
    """Continue an existing session."""
    db = get_db()

    session_id = args.session
    if not session_id:
        session = db.get_active_session()
        if not session:
            print("❌ No active session to continue")
            print("   Start a new session with: ai-relay start")
            return 1
        session_id = session.id
    else:
        session = db.get_session(session_id)
        if not session:
            print(f"❌ Session not found: {session_id}")
            return 1

    print(f"[AI-Relay] Continuing session: {session.id}")

    # Get provider
    provider_name = args.provider or session.last_provider
    provider = get_provider(provider_name)

    if not provider or not provider.is_installed():
        # Try to find alternative
        provider_name = db.get_best_available_provider()
        if provider_name:
            provider = get_provider(provider_name)

    if not provider:
        print("❌ No providers available")
        return 1

    # Check if we need to handoff
    if provider_name != session.last_provider:
        print(f"[AI-Relay] Switching from {session.last_provider} to {provider_name}")
        context = get_injectable_context(session.id, "standard", db)
        if context:
            provider.inject_context(context, "auto")
            db.log_handoff(session.id, session.last_provider, provider_name,
                          estimate_tokens(context), "auto")
            db.update_session(session.id, last_provider=provider_name)

    print(f"[AI-Relay] Launching {provider.display_name}...")
    print()

    provider.launch(wrapped=True)
    return 0


def cmd_sessions(args):
    """List recent sessions."""
    db = get_db()

    sessions = db.list_sessions(limit=args.limit, include_completed=args.all)

    if not sessions:
        print("No sessions found")
        return 0

    print("\n" + "=" * 60)
    print("  Recent Sessions")
    print("=" * 60 + "\n")

    for s in sessions:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(s.created_at)
        hours = age.total_seconds() / 3600

        if hours < 1:
            age_str = f"{int(age.total_seconds() / 60)}m ago"
        elif hours < 24:
            age_str = f"{hours:.1f}h ago"
        else:
            age_str = f"{int(hours / 24)}d ago"

        status_icon = "●" if s.status == "active" else "○"
        print(f"  {status_icon} {s.id}  {s.last_provider:10}  {age_str:12}  {s.total_tokens_used:,} tokens")
        print(f"    {s.project_path}")
        print()

    print("=" * 60 + "\n")
    return 0


def cmd_export(args):
    """Export session context to file or stdout."""
    db = get_db()

    session_id = args.session
    if not session_id:
        session = db.get_active_session()
        if session:
            session_id = session.id

    if not session_id:
        print("❌ No session to export", file=sys.stderr)
        return 1

    context = get_injectable_context(session_id, args.compression or "standard", db)
    if not context:
        print("❌ No context found", file=sys.stderr)
        return 1

    if args.output:
        Path(args.output).write_text(context)
        print(f"Exported to {args.output}", file=sys.stderr)
    else:
        print(context)

    return 0


def cmd_compress(args):
    """Re-compress the current session's context."""
    db = get_db()

    session_id = args.session
    if not session_id:
        session = db.get_active_session()
        if session:
            session_id = session.id

    if not session_id:
        print("❌ No session specified")
        return 1

    level = args.level or "standard"
    context = get_injectable_context(session_id, level, db)

    if not context:
        print("❌ No context to compress")
        return 1

    tokens = estimate_tokens(context)
    print(f"[AI-Relay] Compressed to {level}: {tokens:,} tokens")

    if args.show:
        print("\n" + "-" * 40)
        print(context)
        print("-" * 40)

    return 0


def cmd_config(args):
    """Configure provider quotas and priorities."""
    db = get_db()

    if args.action == "set":
        quota = ProviderQuota(
            provider=args.provider,
            daily_limit=args.limit,
            used_today=0,
            reset_at=(datetime.now(timezone.utc) + timedelta(days=1)).replace(
                hour=0, minute=0, second=0
            ).isoformat(),
            priority=args.priority or 10,
            is_available=True
        )
        db.set_quota(quota)
        print(f"✓ Configured {args.provider}: limit={args.limit}, priority={args.priority or 10}")

    elif args.action == "list":
        quotas = db.list_quotas()
        if not quotas:
            print("No providers configured. Use 'ai-relay config set <provider> --limit N'")
            return 0

        print("\nProvider Configuration:\n")
        for q in quotas:
            limit_str = str(q.daily_limit) if q.daily_limit else "unlimited"
            print(f"  {q.provider:15} limit={limit_str:10} priority={q.priority} used={q.used_today}")
        print()

    return 0


# =============================================================================
# Main Entry Point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        prog="ai-relay",
        description="AI-Relay - Cross-Provider Context Continuity"
    )
    parser.add_argument("--version", action="version", version="ai-relay 0.1.0")

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # status
    p_status = subparsers.add_parser("status", help="Show status of providers and session")
    p_status.add_argument("--json", action="store_true", help="Output as JSON for VS Code extension")

    # capture
    p_capture = subparsers.add_parser("capture", help="Capture context from a provider")
    p_capture.add_argument("--provider", "-p", help="Provider to capture from")
    p_capture.add_argument("--compression", "-c", choices=COMPRESSION_LEVELS.keys(),
                          help="Compression level")
    p_capture.add_argument("--show", "-s", action="store_true", help="Show captured context")

    # inject
    p_inject = subparsers.add_parser("inject", help="Inject context into a provider")
    p_inject.add_argument("--provider", "-p", help="Target provider")
    p_inject.add_argument("--session", help="Session ID to inject from")
    p_inject.add_argument("--compression", "-c", choices=COMPRESSION_LEVELS.keys())
    p_inject.add_argument("--method", "-m", choices=["auto", "system_prompt", "project_file", "first_message"])
    p_inject.add_argument("--launch", "-l", action="store_true", help="Launch provider after injection")
    p_inject.add_argument("--direct", action="store_true", help="Launch without wrapper")

    # handoff
    p_handoff = subparsers.add_parser("handoff", help="Quick handoff to another provider")
    p_handoff.add_argument("provider", nargs="?", help="Target provider")
    p_handoff.add_argument("--compression", "-c", choices=COMPRESSION_LEVELS.keys())
    p_handoff.add_argument("--reason", help="Handoff reason")
    p_handoff.add_argument("--no-launch", action="store_true", help="Don't launch after handoff")

    # start
    p_start = subparsers.add_parser("start", help="Start a new relay session")
    p_start.add_argument("--provider", "-p", help="Provider to start with")

    # continue
    p_continue = subparsers.add_parser("continue", help="Continue an existing session")
    p_continue.add_argument("--session", "-s", help="Session ID")
    p_continue.add_argument("--provider", "-p", help="Provider to use")

    # sessions
    p_sessions = subparsers.add_parser("sessions", help="List recent sessions")
    p_sessions.add_argument("--limit", "-n", type=int, default=10)
    p_sessions.add_argument("--all", "-a", action="store_true", help="Include completed")

    # export
    p_export = subparsers.add_parser("export", help="Export session context")
    p_export.add_argument("--session", "-s", help="Session ID")
    p_export.add_argument("--compression", "-c", choices=COMPRESSION_LEVELS.keys())
    p_export.add_argument("--output", "-o", help="Output file")

    # compress
    p_compress = subparsers.add_parser("compress", help="Re-compress session context")
    p_compress.add_argument("--session", "-s", help="Session ID")
    p_compress.add_argument("--level", "-l", choices=COMPRESSION_LEVELS.keys())
    p_compress.add_argument("--show", action="store_true")

    # config
    p_config = subparsers.add_parser("config", help="Configure providers")
    p_config.add_argument("action", choices=["set", "list"])
    p_config.add_argument("provider", nargs="?")
    p_config.add_argument("--limit", type=int, help="Daily message limit")
    p_config.add_argument("--priority", type=int, help="Priority (lower=higher)")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    # Dispatch to command handler
    commands = {
        "status": cmd_status,
        "capture": cmd_capture,
        "inject": cmd_inject,
        "handoff": cmd_handoff,
        "start": cmd_start,
        "continue": cmd_continue,
        "sessions": cmd_sessions,
        "export": cmd_export,
        "compress": cmd_compress,
        "config": cmd_config,
    }

    handler = commands.get(args.command)
    if handler:
        return handler(args)
    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
