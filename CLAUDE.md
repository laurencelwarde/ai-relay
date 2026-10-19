# AI-Relay - Cross-Provider Context Continuity

## Project Overview
CLI tool and VS Code extension for seamless context handoff between AI coding assistants (Claude, Codex, Grok, Gemini, Ollama) when quotas run out.

## Quick Reference

### Run CLI
```bash
python ai_relay.py status          # Show provider status
python ai_relay.py capture         # Capture context from provider
python ai_relay.py inject          # Inject context into provider
python ai_relay.py handoff         # Quick handoff to another provider
python ai_relay.py start           # Start new relay session
python ai_relay.py continue        # Continue existing session
python ai_relay.py config list     # List provider quotas
python ai_relay.py config set claude --limit 100  # Set quota
```

### Test Commands
```bash
python ai_relay.py status --json   # JSON output for VS Code extension
python -c "from core.ratelimit import RateLimitDetector; print('OK')"  # Test imports
```

### Build VS Code Extension
```bash
cd vscode-extension && npm install && npm run compile && npm run package
```

## Project Structure
```
C:\AI-RELAY\
├── ai_relay.py           # Main CLI entry point
├── core/
│   ├── database.py       # SQLite storage (sessions, snapshots, quotas)
│   ├── context.py        # Context compression (full/standard/minimal/micro)
│   └── ratelimit.py      # Rate limit detection and auto-handoff
├── providers/
│   ├── base.py           # ProviderAdapter abstract base class
│   ├── claude.py         # Claude Code adapter
│   ├── codex.py          # Codex CLI adapter
│   ├── grok.py           # Grok CLI adapter
│   ├── gemini.py         # Gemini CLI adapter
│   └── ollama.py         # Ollama local adapter (fallback)
├── vscode-extension/
│   ├── src/extension.ts  # VS Code extension entry
│   ├── src/sidePanel.ts  # Sidebar UI with provider toggles
│   └── package.json      # Extension manifest
└── media/
    ├── ai-relay-icon.svg # Sidebar icon
    └── ai-relay-logo.svg # Full logo
```

## Key Classes

### ProviderAdapter (providers/base.py)
Abstract base for all provider adapters:
- `is_installed()` - Check if CLI is available
- `check_auth()` - Check authentication
- `check_quota()` - Get quota status
- `capture_session()` - Capture context from session
- `inject_context()` - Inject context for handoff
- `launch()` - Launch CLI (wrapped or direct)

### RelayDatabase (core/database.py)
SQLite storage for:
- Sessions (id, project_path, last_provider, tokens)
- ContextSnapshots (transcript, file_changes, compression)
- ProviderQuotas (daily_limit, used_today, priority)
- HandoffLog (from/to provider, context_tokens, reason)

### RateLimitDetector (core/ratelimit.py)
Monitors output for rate limit patterns and triggers handoffs.

## GitHub Issues Status
- Closed: #1-8, #10-13, #17 (core CLI, adapters, VS Code sidebar)
- Open: #9, #14-16, #18-21 (AI summarization, VS Code enhancements, advanced features)

## Development Notes

### Windows Compatibility
- PTY imports are conditional (`if sys.platform != "win32"`)
- UTF-8 encoding set for console output
- Use forward slashes in Bash tool paths

### Adding a New Provider
1. Create `providers/newprovider.py` extending `ProviderAdapter`
2. Set `name`, `display_name`, `cli_command`, `max_context`, `priority`
3. Implement `check_auth()`, `check_quota()`, `capture_session()`, `inject_context()`, `launch()`
4. Register in `PROVIDERS` dict in `ai_relay.py`

### Compression Levels
- `full`: 50K tokens, last 100 messages, all metadata
- `standard`: 15K tokens, last 10 messages (default)
- `minimal`: 4K tokens, last 5 messages, no task state
- `micro`: 1K tokens, last 2 messages, file changes only

## Related Projects
- `C:\AI COUNCIL` - AI Council multi-model review (UI patterns reference)
