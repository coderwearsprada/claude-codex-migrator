# code-agent-migrator

A single-file Python script that migrates settings and custom configuration
between [Claude Code](https://claude.com/claude-code) (`~/.claude`),
[Codex CLI](https://github.com/openai/codex) (`~/.codex`), and
[Cursor](https://cursor.com) (`~/.cursor`), in any pairwise direction —
with an upfront backup of every file it will touch and a `--restore`
command to undo a run.

Requires Python 3.9+. No third-party dependencies. On 3.11+ it uses the
stdlib `tomllib`; on 3.9/3.10 it falls back to a small bundled TOML reader
covering the subset Codex's `config.toml` uses.

**Tested against:** Claude Code `2.1.150`, Codex CLI `0.133.0`, and Cursor
(MCP `mcp.json` + `.cursor/rules/*.mdc` schemas as of 2026-05). The script
reads documented config schemas, so minor version bumps should keep working;
if a future release renames or removes a key, the migrator will flag it as
"not translated" in the report rather than corrupt your config.

## Usage

```bash
# Any pairwise direction between {claude, codex, cursor}.
python3 migrate.py --from claude --to codex
python3 migrate.py --from cursor --to claude
python3 migrate.py --from codex  --to cursor

# Legacy --direction is still accepted as a shorthand
python3 migrate.py --direction claude-to-codex

# Project-level instead of user-level (also accepts --scope both)
python3 migrate.py --from claude --to cursor --scope project

# Explicit paths (overrides --scope)
python3 migrate.py --from claude --to cursor \
    --claude-dir /path/to/.claude --cursor-dir /path/to/.cursor

# Preview without writing anything
python3 migrate.py --from claude --to cursor --dry-run

# Revert the most recent migration (or pass a specific backup directory)
python3 migrate.py --restore
python3 migrate.py --restore /path/to/backups/pre-migrate-YYYYMMDD-HHMMSS
```

### Flags

| Flag | Meaning |
|---|---|
| `--from {claude,codex,cursor}` / `--to {claude,codex,cursor}` | Source and destination tools. Required unless `--restore` is given. |
| `--direction VALUE` | Backward-compat shorthand for `--from`/`--to`, e.g. `claude-to-codex` or `cursor-to-claude`. |
| `--restore [BACKUP_DIR]` | Reverse a previous migration. Omit to use the latest backup found under any tool's backups dir. |
| `--scope {user,project,both}` | Which config scope(s) to migrate (default: `user`). |
| `--claude-dir PATH` / `--codex-dir PATH` / `--cursor-dir PATH` | Explicit config dirs; overrides `--scope`. |
| `--dry-run` | Print the plan and report, write nothing. |
| `--merge` / `--overwrite` | Merge into existing destination files where sensible (default), or replace outright. Backups happen either way. |
| `--no-backup` | Skip the upfront backup (and disable `--restore` for this run). Not recommended. |
| `--no-interactive` | Don't prompt for Tier B confirmations; combine with `--apply-lossy`/`--skip-lossy`. |
| `--apply-lossy=IDS` / `--skip-lossy=IDS` | Comma-separated Tier B option IDs (or `all`). IDs: `permissions`, `sandbox`, `hooks`, `notify`, `agents`, `skills`, `profiles`, `agents_cursor`, `skills_cursor`, `commands_cursor`, `prompts_cursor`. |

After each run the script writes `MIGRATION_REPORT.md` at the destination,
split into: migrated cleanly (Tier A), migrated with loss (Tier B, user-
confirmed), skipped by user choice, and not translated (no equivalent).

## What gets translated

### Tier A — clean, always applied

**Claude Code ↔ Codex CLI**

| Claude Code                            | Codex CLI                                  |
|----------------------------------------|--------------------------------------------|
| `CLAUDE.md`                            | `AGENTS.md`                                |
| `commands/*.md`                        | `prompts/*.md`                             |
| `settings.json:model`                  | `config.toml:model`                        |
| `settings.json:mcpServers` (stdio)     | `config.toml:[mcp_servers.*]`              |
| `settings.json:env`                    | `config.toml:[shell_environment_policy] set` |
| `settings.json:effortLevel`            | `config.toml:model_reasoning_effort`       |
| `outputStyle` file contents *(c→x only)*  | fenced block inside `AGENTS.md`         |
| fenced block inside `CLAUDE.md` *(x→c only)* | `config.toml:instructions`           |

**Cursor ↔ Claude / Codex**

| Cursor                                 | Claude Code              | Codex CLI                   |
|----------------------------------------|--------------------------|-----------------------------|
| `<root>/mcp.json:mcpServers`           | `settings.json:mcpServers` | `config.toml:[mcp_servers.*]` |
| `.cursor/rules/*.mdc` + `.cursorrules` | `CLAUDE.md`              | `AGENTS.md`                 |

Cursor user scope (`~/.cursor`) only has global MCP — Cursor has no
user-level rules file. Project-scope rules go to/from `<project>/.cursor/`.
The legacy `.cursorrules` (plain markdown at project root) is read on the
way out and re-emitted as a single `.cursor/rules/_cursorrules_legacy.mdc`
on the way in. Rule frontmatter (`description`, `globs`, `alwaysApply`)
rides along in a fenced HTML-comment block inside `CLAUDE.md`/`AGENTS.md`
so cursor→claude→cursor (or cursor→codex→cursor) round-trips preserve it
verbatim.

Notes:

- Slash-command frontmatter `description` and `argument-hint` ride along
  inside a `<!-- migrator:meta ... -->` comment so they survive a c→x→c
  round-trip byte-for-byte. Other frontmatter keys (`model`,
  `allowed-tools`, …) are dropped and logged.
- Codex `instructions` (TOML string) and Claude `outputStyle` files
  (markdown) are different shapes for similar things, so they're embedded
  inside the target's instruction document as a `<!-- migrator:begin ... -->`
  fenced block that the reverse direction can unwrap.
- MCP server transports: **stdio** transfers everywhere. **SSE / HTTP**
  MCP servers transfer between Claude Code and Cursor (both support them),
  but are skipped going to Codex (stdio-only) with a report note.
- Effort levels (Claude `effortLevel` ↔ Codex `model_reasoning_effort`)
  map as `max ↔ high`, `minimal → low`, others 1:1. Cursor has no
  equivalent reasoning-effort knob, so this field is reported but not
  carried.

### Tier B — lossy, user-confirmed

These don't have an exact equivalent on the other side. The preflight scan
shows a one-line preview and rationale for each detected item and lets you
accept or skip per-item (interactively, or via `--apply-lossy`/`--skip-lossy`).

| ID | Translation | Why lossy |
|---|---|---|
| `permissions` | Claude `permissions` → Codex `sandbox_mode` + `approval_policy` + `sandbox_workspace_write` | Per-tool regex patterns collapsed into coarse sandbox modes; `Write()` patterns become `writable_roots`; `WebFetch`/`WebSearch` deny becomes `network_access=false`. |
| `sandbox` | Codex `sandbox_mode`/`approval_policy` → Claude `permissions.allow`/`deny` | Coarse modes expanded into Claude wildcard patterns. Round-trip is semantic, not byte-identical. |
| `hooks` | Claude `hooks.Notification`/`Stop` → Codex `notify` | Only the notification-style hook events translate; `PreToolUse`/`PostToolUse`/`UserPromptSubmit`/`SessionStart`/`SessionEnd`/`PreCompact` are Claude-only and get listed in the report. The shell command is wrapped as `["/bin/sh", "-c", ...]`. |
| `notify` | Codex `notify` → Claude `hooks.Notification` | Single command with no matcher. |
| `agents` | Claude `agents/*.md` → Codex `prompts/agent-*.md` (one-way) | Codex has no subagent runtime; each subagent file is flattened into a plain prompt with a header comment preserving the original frontmatter. |
| `skills` | Claude `skills/*/SKILL.md` → Codex `prompts/skill-*.md` (one-way) | `SKILL.md` becomes a flat prompt; bundled assets are not migrated and auto-discovery is lost. |
| `profiles` | Codex `[profiles.NAME]` → `~/.claude/profiles/NAME.settings.json` (one-way) | Claude has no profile runtime; each Codex profile is materialized as a standalone settings file you can copy over `settings.json` to activate. |
| `agents_cursor` | Claude `agents/*.md` → `.cursor/rules/agent-*.mdc` (one-way) | Cursor has no subagent runtime. Each agent.md becomes an `alwaysApply:false` rule — content survives, subagent invocation semantics don't. |
| `skills_cursor` | Claude `skills/*/SKILL.md` → `.cursor/rules/skill-*.mdc` (one-way) | Cursor has no skills runtime. `SKILL.md` becomes an `alwaysApply:false` rule; bundled assets are not migrated and auto-discovery is lost. |
| `commands_cursor` | Claude `commands/*.md` → `.cursor/rules/command-*.mdc` (one-way) | Cursor has no slash-command equivalent. Commands become `alwaysApply:false` rules — content is loadable but won't be invokable as `/name`. |
| `prompts_cursor` | Codex `prompts/*.md` → `.cursor/rules/prompt-*.mdc` (one-way) | Cursor has no on-demand prompt invocation. Codex prompts become `alwaysApply:false` rules — content is loadable but loses its on-demand semantics. |

Note that cursor *→* claude/codex doesn't need any Tier B options: rules
+ MCP are clean Tier A translations and rule frontmatter
(`description`/`globs`/`alwaysApply`) round-trips via fenced metadata
inside `CLAUDE.md`/`AGENTS.md`. The lossy direction is only when going
*to* Cursor with content (subagents, skills, slash commands) Cursor has
no runtime for.

### Tier C — not translated

Listed in `MIGRATION_REPORT.md` so you know to recreate them by hand:

- **Claude-only:** `statusLine`, `plugins/`, theme, slash-command `model`/`allowed-tools` frontmatter, and the hook event types listed above. When migrating to Cursor, also: `hooks`, `permissions`, `outputStyle` (agents/skills/commands have Tier B options).
- **Codex-only:** `model_provider(s)`, `tools.web_search`, `disable_response_storage` / history persistence, `tui` settings, `hide_agent_reasoning`, `project_doc_max_bytes`. When migrating to Cursor, also: `approval_policy`, `sandbox_mode`, `sandbox_workspace_write`, `shell_environment_policy`, `profiles`, `model_reasoning_effort`, `notify` (Codex prompts have a Tier B option).
- **Cursor-only:** Cursor IDE settings (`User/settings.json`), keybindings, extensions list, notepads, composer history — all out of scope (IDE config, not agent config). Cursor MCP entries with `type: "sse"` or HTTP URLs are kept verbatim into Claude, but skipped going to Codex (which only supports stdio).

## What is never touched

The script ignores state, secrets, and caches on the source side, including:

- **Claude:** `.credentials.json`, `history.jsonl`, `sessions/`, `projects/`,
  `file-history/`, `cache/`, `paste-cache/`, `shell-snapshots/`,
  `telemetry/`, `mcp-needs-auth-cache.json`
- **Codex:** `auth.json`, `history.jsonl`, `sessions/`, `log/`,
  `version.json`
- **Cursor:** the OS-specific user settings dir (`User/settings.json`,
  keybindings, extensions, workspace storage) — anywhere outside
  `<root>/mcp.json` and `<root>/rules/*.mdc`

## How a migration runs

1. **Preflight scan** — detect Tier B translations applicable to your
   source and confirm each one. Tier A items are always applied.
2. **Plan pass** — walk the migration once without writing anything,
   collecting the complete list of destination files that will be touched.
3. **Backup** — copy every planned destination that already exists into
   `<dst>/backups/pre-migrate-<timestamp>/`, preserving relative layout,
   and write a `manifest.json` that `--restore` later reads. Files the
   migration will *create* (vs. modify) are recorded as
   `existed_before: false` so restore can delete them on revert.
4. **Confirm** — show the list of planned changes plus the backup
   location and ask one final time.
5. **Apply** — write for real; produce `MIGRATION_REPORT.md` at the
   destination.

## Restoring a migration

`--restore` reverses a previous migration using the backup manifest: files
that existed before are copied back from the backup byte-for-byte, and
files the migration created are deleted.

```bash
python3 migrate.py --restore                    # latest backup found
python3 migrate.py --restore /path/to/backup    # specific run
python3 migrate.py --restore --dry-run          # preview only
```

## Tests

```bash
python3 -m unittest discover -s tests
```

56 tests, stdlib-only. They cover the TOML writer, frontmatter and
fenced-block round-trips, MCP normalization for all three tools, every
Tier A direction (claude↔codex, claude↔cursor, codex↔cursor), the
slash-command `description`/`argument-hint` round-trip, MDC frontmatter
+ legacy `.cursorrules` parsing, every Tier B heuristic, the plan-mode
contract, the backup-then-restore round-trip, and a full
cursor→claude→cursor metadata round-trip. Verified on Python 3.9 and 3.13.

## License

MIT
