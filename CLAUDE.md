# AI-Relay - Cross-Provider Context Continuity

## Project Overview
CLI tool and VS Code extension for seamless context handoff between LLM providers (Claude Code, OpenAI Codex, Grok CLI, Gemini, Ollama).

## Build Commands
```bash
# Python CLI
pip install -e .

# VS Code extension (Phase 3)
cd vscode-extension && npm run compile && npm run package
```

## Key Files
- `ai_relay.py` - Main CLI entry point
- `core/context.py` - Context capture/compression
- `core/database.py` - SQLite operations
- `core/handoff.py` - Handoff orchestration
- `core/summarizer.py` - AI compression engine
- `providers/*.py` - Provider adapters

## Architecture
See `SPEC.md` for full architecture and implementation plan.

## Related Projects
- `C:\AI COUNCIL` - AI Council multi-model review (shares architecture patterns)

## Current Phase
Phase 1: Core CLI (MVP)
- [ ] SQLite session/context storage
- [ ] Manual capture command (`ai-relay capture`)
- [ ] Manual inject command (`ai-relay inject --provider codex`)
- [ ] Basic compression (last N messages + file list)
- [ ] Claude + Codex adapters
