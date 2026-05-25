# claude-codex-migrator

A single-file Python script that migrates settings and custom configuration
between [Claude Code](https://claude.com/claude-code) (`~/.claude`) and
[Codex CLI](https://github.com/openai/codex) (`~/.codex`), in either direction.

Requires Python 3.11+ (uses `tomllib` from the standard library). No third-party
dependencies.

## Usage

```bash
# User-level config (default): ~/.claude  <->  ~/.codex
python3 migrate.py --direction claude-to-codex
python3 migrate.py --direction codex-to-claude

# Project-level: ./.claude  <->  ./.codex  (plus ./CLAUDE.md / ./AGENTS.md)
python3 migrate.py --direction claude-to-codex --scope project

# Both user and project scopes in one run
python3 migrate.py --direction claude-to-codex --scope both

# Explicit paths (overrides --scope)
python3 migrate.py --direction claude-to-codex \
    --claude-dir /path/to/.claude --codex-dir /path/to/.codex

# Preview without writing anything
python3 migrate.py --direction claude-to-codex --dry-run
```

Other flags:

- `--merge` (default) — merge into existing destination files where sensible;
  conflicts are backed up first.
- `--overwrite` — replace destination files outright (after backup).
- `--no-backup` — skip backups (not recommended).
- `--include-agents` — best-effort, lossy conversion of Claude subagents
  (`agents/*.md`) into Codex prompts (`prompts/agent-*.md`).

After each run the script writes `MIGRATION_REPORT.md` at the destination
listing what was migrated, what was skipped, and where backups live.

## What maps

| Claude Code                     | Codex CLI                          |
|---------------------------------|------------------------------------|
| `CLAUDE.md`                     | `AGENTS.md`                        |
| `commands/*.md`                 | `prompts/*.md`                     |
| `settings.json:mcpServers`      | `config.toml:[mcp_servers.*]`      |
| `settings.json:model`           | `config.toml:model`                |
| `agents/*.md` (opt-in, lossy)   | `prompts/agent-*.md`               |

YAML frontmatter on slash commands is stripped (Codex prompts don't use it).
SSE / HTTP MCP servers on the Claude side are flagged as unsupported in Codex.

## What does **not** map

These are listed in the migration report so you know what to recreate by hand:

- **Claude-only:** hooks, permissions, `statusLine`, env, `skills/`,
  `plugins/`, output styles, slash-command frontmatter (`allowed-tools`, etc.)
- **Codex-only:** profiles, `approval_policy`, `sandbox_mode` /
  `sandbox_workspace_write`, `shell_environment_policy`, `model_provider(s)`,
  `reasoning_effort`, `tools.web_search`, history persistence

## What is never touched

The script ignores state, secrets, and caches on the source side, including:

- Claude: `history.jsonl`, `sessions/`, `projects/`, `file-history/`, `cache/`,
  `paste-cache/`, `shell-snapshots/`, `telemetry/`, `.credentials.json`,
  `mcp-needs-auth-cache.json`
- Codex: `auth.json`, `history.jsonl`, `sessions/`, `log/`, `version.json`

## License

MIT
