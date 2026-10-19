# AI-Relay - Cross-Provider Context Continuity

## Overview

AI Relay enables seamless context handoff between different LLM CLIs and subscriptions (Claude Code, OpenAI Codex, Grok CLI, etc.) without losing conversation history or project understanding.

**Problem:** When you hit rate limits, token caps, or subscription boundaries on one provider, switching to another requires manually copying context - losing nuance, tool states, and conversation flow.

**Solution:** AI-Relay captures, compresses, and transfers session context between providers automatically.

**Note:** AI-Relay is a standalone application. While it may borrow UI patterns from AI-Council, they are separate products with no runtime integration.

---

## How Handover Actually Works

The key question: How does AI-Relay capture context from Claude CLI and hand off to Codex when tokens run out?

### Wrapper Mode (Primary)

AI-Relay wraps the target CLI, intercepting all I/O:

```bash
$ ai-relay start
[AI-Relay] Starting session rel_abc123
[AI-Relay] Checking providers...
  Claude Code: 950/1000 msgs (Pro) - SELECTED
  Codex CLI:   Available
  Grok CLI:    480/500 msgs
[AI-Relay] Launching Claude Code (wrapped)...

# === Claude CLI starts, user works normally ===

You: Help me refactor the auth system
Claude: I'll help you refactor...
[... 45 minutes of work ...]

# === Claude returns rate limit ===

Claude: Error: Rate limit exceeded. Please try again later.

[AI-Relay] Rate limit detected!
[AI-Relay] Capturing context... (12,847 tokens)
[AI-Relay] Compressing to Standard... (4,231 tokens)
[AI-Relay]
[AI-Relay] Handoff options:
[AI-Relay]   1. Codex CLI (unlimited) - RECOMMENDED
[AI-Relay]   2. Grok CLI (20 msgs left)
[AI-Relay]   3. Save & exit
[AI-Relay]
[AI-Relay] Select [1]: 1

[AI-Relay] Injecting context into Codex...
[AI-Relay] Launching Codex CLI...

# === Codex starts with full context ===

Codex: Continuing from your Claude session...
       You were refactoring the auth system to JWT.
       Last action: Created AuthContext.tsx
       Next: Add login form component

       Ready to continue. What's next?
```

**How the wrapper works:**
1. `ai-relay start` spawns Claude CLI as a subprocess (PTY)
2. All stdin/stdout passes through AI-Relay
3. AI-Relay logs the full conversation in real-time
4. When rate limit pattern detected in stdout, handoff triggers
5. Context compressed and injected into next provider's launch

### Manual Mode (Fallback)

If user started Claude directly (not through wrapper):

```bash
# Terminal 1: User is in raw Claude CLI, hits rate limit

# Terminal 2:
$ ai-relay capture
[AI-Relay] No wrapped session found.
[AI-Relay] Options:
[AI-Relay]   1. Paste conversation from clipboard
[AI-Relay]   2. Import from Claude session file (~/.claude/...)
[AI-Relay]   3. Describe context manually
[AI-Relay] Select [1]: 2

[AI-Relay] Found 3 recent Claude sessions:
[AI-Relay]   1. auth-refactor (2 hours ago, 12K tokens)
[AI-Relay]   2. bug-fix-login (yesterday)
[AI-Relay]   3. new-feature (3 days ago)
[AI-Relay] Select [1]: 1

[AI-Relay] Captured session "auth-refactor" (12,847 tokens)
[AI-Relay] Compressed to Standard (4,231 tokens)

$ ai-relay handoff codex
[AI-Relay] Launching Codex with context...
```

### Context Injection Methods

How context gets into the new CLI:

**Method 1: System Prompt (Codex/Grok)**
```
You are continuing a development session relayed from Claude Code.

## Session Context
- Working on: Auth system refactor to JWT
- Project: /home/user/myapp
- Key decisions: Using RS256, refresh tokens, 15min expiry
- Last action: Created AuthContext.tsx
- Next step: Add login form component

## Recent Conversation Summary
[User asked about JWT vs sessions, chose JWT for statelessness]
[Created AuthContext with useContext hook]
[Discussed token storage - chose httpOnly cookies]

## Modified Files
- src/contexts/AuthContext.tsx (new)
- src/hooks/useAuth.ts (new)

## Current Task
Add login form component connected to /api/auth/login

Continue from here.
```

**Method 2: Project File (CLAUDE.md equivalent)**
Append to project's instruction file, which CLI reads on start.

**Method 3: First Message**
AI-Relay sends context as first user message in new session.

---

## Architecture

### Core Components

```
+------------------+     +------------------+     +------------------+
|   Claude Code    |     |   Relay Core     |     |   Codex CLI      |
|   (Provider A)   |<--->|                  |<--->|   (Provider B)   |
+------------------+     |  - Context DB    |     +------------------+
                         |  - Summarizer    |
+------------------+     |  - Provider Mgr  |     +------------------+
|    Grok CLI      |<--->|  - Quota Tracker |<--->|   Gemini CLI     |
|   (Provider C)   |     |  - Hook System   |     |   (Provider D)   |
+------------------+     +------------------+     +------------------+
```

### 1. Context Capture Layer

Intercepts and stores:
- Full conversation transcript (messages + tool calls)
- File modifications made during session
- Current working directory and project structure
- Active task list / todo state
- Recent tool outputs (git status, test results, etc.)
- User preferences expressed during session

### 2. Context Compression Engine

Uses a fast/cheap model (Gemini Flash, Haiku, local Ollama) to:
- Summarize conversation into key points
- Extract critical decisions and constraints
- Identify unfinished work and next steps
- Compress file diffs into semantic descriptions
- Prioritize recent context over stale

**Compression Levels:**
| Level | Tokens | Use Case |
|-------|--------|----------|
| Full | ~50K | Same provider, session resume |
| Standard | ~15K | Cross-provider handoff |
| Minimal | ~4K | Emergency/rate-limited handoff |
| Micro | ~1K | Quick question continuation |

### 3. Provider Adapters

Standardized interface for each CLI:

```python
class ProviderAdapter:
    name: str                    # "claude", "codex", "grok"
    cli_command: str             # "claude", "codex", "grok"
    context_injection: str       # How to pass context (--resume, env var, stdin)
    max_context: int             # Token limit for this provider

    def check_available() -> bool
    def check_quota() -> QuotaStatus
    def inject_context(context: RelayContext) -> None
    def capture_session() -> SessionState
    def launch_interactive() -> Process
```

### 4. Relay Database (SQLite)

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    project_path TEXT,
    created_at TIMESTAMP,
    last_provider TEXT,
    total_tokens_used INTEGER
);

CREATE TABLE context_snapshots (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    provider TEXT,
    captured_at TIMESTAMP,
    full_transcript TEXT,        -- JSON: all messages
    compressed_summary TEXT,     -- AI-generated summary
    file_changes TEXT,           -- JSON: modified files
    task_state TEXT,             -- JSON: todo list
    compression_level TEXT
);

CREATE TABLE provider_quotas (
    provider TEXT PRIMARY KEY,
    daily_limit INTEGER,
    used_today INTEGER,
    reset_at TIMESTAMP,
    priority INTEGER             -- Lower = preferred
);

CREATE TABLE handoff_log (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    from_provider TEXT,
    to_provider TEXT,
    handoff_at TIMESTAMP,
    context_tokens INTEGER,
    reason TEXT                  -- "quota", "manual", "error", "scheduled"
);
```

---

## User Experience

### CLI Usage

```bash
# Start a new relay session (picks best available provider)
ai-relay start

# Start with specific provider
ai-relay start --provider claude

# Continue existing session (auto-picks next provider if needed)
ai-relay continue

# Manual handoff to specific provider
ai-relay handoff codex

# Check provider status
ai-relay status
# Output:
#   Claude Code:  [====----] 450/1000 msgs today (Pro)
#   Codex CLI:    [--------] Available (subscription active)
#   Grok CLI:     [==------] 200/500 msgs today
#   Gemini:       [--------] Available (free tier)
#
#   Current session: abc123 (started 2h ago with Claude)
#   Context size: 12,847 tokens (Standard compression)

# Force context refresh/recompression
ai-relay compress --level minimal

# Export session for manual use
ai-relay export --format markdown > session-context.md
```

### Automatic Handoff Triggers

1. **Quota Exhaustion**: Provider returns rate limit error
2. **Context Overflow**: Approaching provider's context limit
3. **Error Recovery**: Provider errors 3x consecutively
4. **Scheduled Rotation**: User-configured time-based switching
5. **Cost Optimization**: Switch to cheaper provider for simple tasks

### Handoff Flow

```
1. User working in Claude Code
   |
2. Claude returns "rate limit" or user types "/ai-relay"
   |
3. Relay captures current session state
   |
4. Compression engine summarizes context
   |
5. Provider manager selects next provider (Codex)
   |
6. Context injected into Codex CLI
   |
7. Codex resumes with: "Continuing session from Claude..."
   |
8. User continues seamlessly
```

---

## Context Injection Methods

### Method 1: System Prompt Injection (Preferred)

```markdown
# Session Context (Relayed from Claude Code)

## Summary
You are continuing a development session. The user was working on...

## Key Decisions Made
- Using React 18 with TypeScript
- API endpoints follow REST conventions
- Tests use Jest + React Testing Library

## Current Task
Implementing user authentication - JWT tokens chosen over sessions.
Last action: Created AuthContext.tsx, about to add login form.

## Recent File Changes
- src/contexts/AuthContext.tsx (new)
- src/hooks/useAuth.ts (new)
- src/api/auth.ts (modified)

## Unfinished Work
- [ ] Add login form component
- [ ] Connect to /api/auth/login endpoint
- [ ] Add protected route wrapper

## User Preferences (this session)
- Prefers functional components
- Asked for minimal comments
- Using Tailwind CSS
```

### Method 2: CLAUDE.md / Project File

Append relay context to project's CLAUDE.md (or equivalent):

```markdown
<!-- RELAY_CONTEXT_START -->
## Active Relay Session: abc123
Last updated: 2024-01-15 14:30:00
Previous provider: Claude Code

[Compressed context here]
<!-- RELAY_CONTEXT_END -->
```

### Method 3: Environment Variable

```bash
RELAY_CONTEXT_FILE=/tmp/ai-relay-abc123.json codex
```

---

## Provider Priority & Selection

### Default Priority (configurable)

1. **Claude Code** - Best for complex reasoning, code architecture
2. **Codex CLI** - Strong for code generation, quick edits
3. **Grok CLI** - Good general purpose, real-time knowledge
4. **Gemini CLI** - Free tier available, good for simple tasks
5. **Ollama (local)** - No quota limits, privacy, offline

### Smart Selection Factors

```python
def select_next_provider(task_complexity: str, context_size: int) -> Provider:
    available = [p for p in providers if p.check_quota().available]

    # Filter by context capacity
    available = [p for p in available if p.max_context >= context_size]

    # For complex architecture decisions, prefer Claude
    if task_complexity == "high" and "claude" in available:
        return "claude"

    # For quick code edits, Codex is efficient
    if task_complexity == "low" and "codex" in available:
        return "codex"

    # Default: highest priority available
    return sorted(available, key=lambda p: p.priority)[0]
```

---

## VS Code Extension (Standalone)

AI-Relay has its own VS Code extension - separate from any other tools.

### Sidebar UI

```
+----------------------------------+
| AI-Relay                         |
+----------------------------------+
| Session: rel_abc123 (2h active)  |
| Context: 12.8K tokens            |
+----------------------------------+
| Providers              Status    |
|   [x] Claude Code      [====--]  |
|   [x] Codex CLI        [------]  |
|   [ ] Grok CLI         [==----]  |
|   [ ] Gemini           [------]  |
|   [ ] Ollama (local)   [------]  |
+----------------------------------+
| [Start Session] [Handoff Now]    |
| [Compress] [Export]              |
+----------------------------------+
```

### Features
- Toggle providers on/off (like AI-Council's model toggles)
- Visual quota bars per provider
- One-click handoff
- Context size indicator
- Compression controls

---

## Configuration

### ~/.ai-relay/config.yaml

```yaml
providers:
  claude:
    command: claude
    priority: 1
    daily_limit: 1000  # messages
    context_max: 100000

  codex:
    command: codex
    priority: 2
    daily_limit: null  # unlimited with subscription
    context_max: 128000

  grok:
    command: grok
    priority: 3
    daily_limit: 500
    context_max: 32000

  ollama:
    command: ollama run llama3.1
    priority: 10  # Fallback
    daily_limit: null
    context_max: 8000
    local: true

compression:
  default_level: standard
  summarizer: gemini-flash  # Cheap/fast model for compression
  preserve_last_n_messages: 10

handoff:
  auto_on_quota: true
  auto_on_error: true
  error_threshold: 3

hooks:
  pre_handoff: ~/.ai-relay/hooks/pre-handoff.sh
  post_handoff: ~/.ai-relay/hooks/post-handoff.sh
```

---

## Implementation Phases

### Phase 1: Core CLI (MVP)
- [ ] SQLite session/context storage
- [ ] Manual capture command (`ai-relay capture`)
- [ ] Manual inject command (`ai-relay inject --provider codex`)
- [ ] Basic compression (last N messages + file list)
- [ ] Claude + Codex adapters

### Phase 2: Automatic Handoff
- [ ] Quota tracking per provider
- [ ] Auto-detect rate limit errors
- [ ] AI-powered context summarization
- [ ] Grok + Gemini adapters
- [ ] `ai-relay start` / `ai-relay continue` commands

### Phase 3: VS Code Integration
- [ ] Add to AI Council sidebar
- [ ] Visual handoff button
- [ ] Provider status indicators
- [ ] Context size visualization

### Phase 4: Advanced Features
- [ ] Ollama adapter (local fallback)
- [ ] Cross-machine sync (optional cloud backup)
- [ ] Team relay (shared sessions)
- [ ] Cost tracking and optimization suggestions
- [ ] Custom provider plugins

---

## File Structure

```
ai-relay/
├── ai_relay.py           # Main CLI entry point
├── core/
│   ├── context.py        # Context capture/compression
│   ├── database.py       # SQLite operations
│   ├── handoff.py        # Handoff orchestration
│   └── summarizer.py     # AI compression engine
├── providers/
│   ├── base.py           # ProviderAdapter base class
│   ├── claude.py         # Claude Code adapter
│   ├── codex.py          # OpenAI Codex adapter
│   ├── grok.py           # Grok CLI adapter
│   ├── gemini.py         # Gemini adapter
│   └── ollama.py         # Local Ollama adapter
├── hooks/
│   └── examples/         # Example pre/post handoff scripts
└── vscode-extension/     # VS Code integration (Phase 3)
    └── src/
        └── relayPanel.ts
```

---

## Example Session Flow

```
$ ai-relay start
[AI-Relay] Starting new session: rel_abc123
[AI-Relay] Selected provider: Claude Code (priority 1, 950/1000 msgs remaining)
[AI-Relay] Launching Claude Code...

> Help me refactor the authentication system to use JWT

[Claude working for 45 minutes, 47 messages...]

[AI-Relay] Warning: Claude quota at 95% (998/1000)
[AI-Relay] Auto-handoff in 2 messages or type /ai-relay to switch now

> /ai-relay

[AI-Relay] Capturing context... (12,847 tokens)
[AI-Relay] Compressing to Standard level... (4,231 tokens)
[AI-Relay] Selecting next provider...
[AI-Relay] Handoff to: Codex CLI (unlimited quota)
[AI-Relay] Injecting context...

--- Codex CLI ---
Continuing relay session rel_abc123 (from Claude Code)

Context summary:
- Refactoring auth to JWT (was session-based)
- Created: AuthContext.tsx, useAuth.ts, auth.ts
- Current: Adding login form component
- Style: Functional components, Tailwind, minimal comments

Ready to continue. What's next?

> Let's add the login form now

[Codex continues seamlessly...]
```

---

## Open Questions

1. **Privacy**: Should context be encrypted at rest?
2. **Sync**: Cloud backup for cross-machine continuity?
3. **Billing**: Track actual API costs across providers?
4. **Plugins**: Allow custom provider adapters?
5. **Teams**: Shared relay sessions for pair programming?

---

## Summary

AI-Relay solves the multi-subscription problem by treating context as a portable asset that moves between providers. Using a wrapper architecture (CLI passthrough with I/O logging), SQLite persistence, and AI-powered compression, it enables seamless handoffs when any single provider's quota is exhausted.

The key insight: **Context is more valuable than any single provider.**
