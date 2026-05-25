#!/usr/bin/env python3
"""
migrate.py — Migrate settings + custom configuration between Claude Code
and Codex CLI, in either direction, for any user.

Requires: Python 3.11+ (uses tomllib).

USAGE
-----
    # User-level (default): ~/.claude  <->  ~/.codex
    python3 migrate.py --direction claude-to-codex
    python3 migrate.py --direction codex-to-claude

    # Project-level: ./.claude  <->  ./.codex  (plus ./CLAUDE.md / ./AGENTS.md)
    python3 migrate.py --direction claude-to-codex --scope project
    python3 migrate.py --direction claude-to-codex --scope both

    # Explicit paths (overrides --scope)
    python3 migrate.py --direction claude-to-codex \
        --claude-dir /path/to/.claude --codex-dir /path/to/.codex

    # Other useful flags
    --dry-run         Show what would change; write nothing.
    --merge           Merge into existing destination files where possible
                      (default). MCP servers + instruction docs are merged;
                      conflicting files are backed up first.
    --overwrite       Replace destination files outright (after backup).
    --no-backup       Skip backups (NOT recommended).
    --include-agents  Convert Claude subagents to Codex prompts (best-effort,
                      lossy — agent frontmatter is preserved as a header).

WHAT MAPS
---------
    CLAUDE.md                <->  AGENTS.md
    commands/*.md            <->  prompts/*.md
    settings.json:mcpServers <->  config.toml:[mcp_servers.*]
    settings.json:model      <->  config.toml:model
    agents/*.md              -->  prompts/agent-*.md   (one-way, opt-in)

WHAT DOES NOT MAP (logged in MIGRATION_REPORT.md at destination root)
---------------------------------------------------------------------
    Claude-only: hooks, permissions, statusLine, env, skills/, plugins/,
                 output styles, slash-command frontmatter (allowed-tools, etc.)
    Codex-only:  profiles, approval_policy, sandbox_mode/workspace_write,
                 shell_environment_policy, model_provider(s), reasoning_effort,
                 tools.web_search, history.persistence

NEVER TOUCHED (source-side state, secrets, caches)
--------------------------------------------------
    Claude: history.jsonl, sessions/, projects/, file-history/, cache/,
            paste-cache/, shell-snapshots/, telemetry/, .credentials.json,
            mcp-needs-auth-cache.json, downloads/, tasks/, session-env/,
            backups/
    Codex:  auth.json, history.jsonl, sessions/, log/, version.json, backups/
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    print("This script requires Python 3.11+ (for tomllib).", file=sys.stderr)
    sys.exit(1)


# ============================================================================
# Constants
# ============================================================================

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)

# Source-side files/dirs that should never be migrated (state, secrets, caches).
CLAUDE_NEVER_TOUCH = {
    ".credentials.json", "history.jsonl", "mcp-needs-auth-cache.json",
    "sessions", "projects", "file-history", "cache", "paste-cache",
    "shell-snapshots", "telemetry", "downloads", "tasks", "session-env",
    "backups", "ide", "todos", "statsig",
}
CODEX_NEVER_TOUCH = {
    "auth.json", "history.jsonl", "version.json", "sessions", "log",
    "backups",
}

# Claude settings keys that have no Codex equivalent.
CLAUDE_UNMAPPABLE_KEYS = (
    "hooks", "permissions", "statusLine", "env", "outputStyle",
    "agentPushNotifEnabled", "remoteControlAtStartup", "effortLevel",
    "autoUpdates", "verbose", "theme", "preferredNotifChannel",
    "enableAllProjectMcpServers", "enabledMcpjsonServers",
    "disabledMcpjsonServers", "alwaysThinkingEnabled",
)

# Codex config keys that have no Claude Code equivalent.
CODEX_UNMAPPABLE_KEYS = (
    "approval_policy", "sandbox_mode", "sandbox_workspace_write",
    "shell_environment_policy", "profiles", "profile",
    "model_provider", "model_providers", "reasoning_effort",
    "reasoning_summary", "tools", "history", "tui",
    "hide_agent_reasoning", "show_raw_agent_reasoning",
    "model_reasoning_effort", "model_reasoning_summary",
    "model_verbosity", "disable_response_storage",
    "notify", "instructions", "project_doc_max_bytes",
)


# ============================================================================
# Helpers
# ============================================================================

def ts() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def strip_frontmatter(text: str) -> tuple[str, str | None]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return text, None
    return text[m.end():], m.group(1)


# ----- Minimal TOML writer (stdlib has tomllib for reading only) ------------

def _toml_escape(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _toml_key(k: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", k):
        return k
    return _toml_escape(k)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return _toml_escape(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(
            f"{_toml_key(k)} = {_toml_value(val)}" for k, val in v.items()
        ) + " }"
    if v is None:
        return _toml_escape("")
    raise TypeError(f"Cannot serialize {type(v).__name__} to TOML")


def _is_simple_inline(d: dict) -> bool:
    """All leaf scalars — render as inline table."""
    return all(not isinstance(v, dict) for v in d.values())


def write_toml(data: dict, path: Path) -> None:
    lines: list[str] = []

    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}

    for k, v in scalars.items():
        lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
    if scalars and tables:
        lines.append("")

    def emit(prefix: str, table: dict) -> None:
        subtables = {k: v for k, v in table.items()
                     if isinstance(v, dict) and not _is_simple_inline(v)}
        own = {k: v for k, v in table.items() if k not in subtables}
        lines.append(f"[{prefix}]")
        for k, v in own.items():
            lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
        lines.append("")
        for sk, sv in subtables.items():
            emit(f"{prefix}.{_toml_key(sk)}", sv)

    for k, v in tables.items():
        emit(_toml_key(k), v)

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


# ============================================================================
# Report + I/O context
# ============================================================================

@dataclass
class Report:
    direction: str
    moved: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self, backup_root: Path | None) -> str:
        def bullets(items: list[str], empty: str = "_(nothing)_") -> list[str]:
            return [f"- {x}" for x in items] if items else [empty]

        parts = [
            f"# Migration report — {self.direction}",
            f"_{dt.datetime.now().isoformat(timespec='seconds')}_",
            "",
            "## Migrated",
            *bullets(self.moved),
            "",
            "## Skipped (no equivalent on destination)",
            *bullets(self.skipped),
            "",
            "## Backups",
        ]
        if backup_root:
            parts.append(f"Existing destination files moved to: `{backup_root}`")
        parts += bullets(self.backups, "_(nothing replaced)_")
        if self.notes:
            parts += ["", "## Notes", *bullets(self.notes)]
        return "\n".join(parts) + "\n"


@dataclass
class Ctx:
    src_root: Path        # source config dir (.claude or .codex)
    dst_root: Path        # destination config dir
    src_doc: Path | None  # optional sibling instruction doc (project scope)
    dst_doc: Path | None
    dry_run: bool
    merge: bool
    backup: bool
    include_agents: bool
    report: Report
    backup_root: Path = field(init=False)

    def __post_init__(self) -> None:
        self.backup_root = self.dst_root / "backups" / f"pre-migrate-{ts()}"


def _ensure_parent(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def _backup_if_exists(ctx: Ctx, path: Path) -> None:
    if not path.exists() or not ctx.backup:
        return
    ctx.backup_root.mkdir(parents=True, exist_ok=True)
    rel = path.name
    dest = ctx.backup_root / rel
    i = 1
    while dest.exists():
        dest = ctx.backup_root / f"{path.stem}.{i}{path.suffix}"
        i += 1
    if ctx.dry_run:
        ctx.report.backups.append(f"would back up: {path} → {dest}")
        return
    shutil.copy2(path, dest)
    ctx.report.backups.append(str(dest))


def _write_text(ctx: Ctx, path: Path, content: str) -> None:
    _backup_if_exists(ctx, path)
    if ctx.dry_run:
        print(f"[dry-run] write {path}")
        return
    _ensure_parent(path)
    path.write_text(content, encoding="utf-8")


def _copy_file(ctx: Ctx, src: Path, dst: Path) -> None:
    _backup_if_exists(ctx, dst)
    if ctx.dry_run:
        print(f"[dry-run] copy {src} → {dst}")
        return
    _ensure_parent(dst)
    shutil.copy2(src, dst)


# ============================================================================
# Instruction docs (CLAUDE.md <-> AGENTS.md)
# ============================================================================

def migrate_doc(ctx: Ctx, src: Path, dst: Path, label: str) -> None:
    if not src.exists():
        return
    if dst.exists() and ctx.merge:
        merged = (
            dst.read_text(encoding="utf-8").rstrip()
            + f"\n\n<!-- Merged from {src.name} on {dt.datetime.now().date()} -->\n\n"
            + src.read_text(encoding="utf-8")
        )
        _write_text(ctx, dst, merged)
        ctx.report.moved.append(f"{label} (merged into existing {dst.name})")
    else:
        _copy_file(ctx, src, dst)
        ctx.report.moved.append(label)


# ============================================================================
# Slash commands <-> prompts
# ============================================================================

def migrate_commands_to_prompts(ctx: Ctx) -> None:
    src_dir = ctx.src_root / "commands"
    if not src_dir.is_dir():
        return
    dst_dir = ctx.dst_root / "prompts"
    for f in sorted(src_dir.rglob("*.md")):
        rel = f.relative_to(src_dir)
        dst = dst_dir / rel
        body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
        if fm:
            keys = re.findall(r"^(\w+):", fm, re.M)
            summary = ", ".join(keys) if keys else "opaque"
            ctx.report.notes.append(
                f"commands/{rel}: dropped frontmatter ({summary})"
            )
        _write_text(ctx, dst, body)
        ctx.report.moved.append(f"commands/{rel} → prompts/{rel}")


def migrate_prompts_to_commands(ctx: Ctx) -> None:
    src_dir = ctx.src_root / "prompts"
    if not src_dir.is_dir():
        return
    dst_dir = ctx.dst_root / "commands"
    for f in sorted(src_dir.rglob("*.md")):
        rel = f.relative_to(src_dir)
        dst = dst_dir / rel
        _copy_file(ctx, f, dst)
        ctx.report.moved.append(f"prompts/{rel} → commands/{rel}")


# ============================================================================
# Subagents (Claude → Codex prompts, best-effort, opt-in)
# ============================================================================

def migrate_agents_to_prompts(ctx: Ctx) -> None:
    if not ctx.include_agents:
        return
    src_dir = ctx.src_root / "agents"
    if not src_dir.is_dir():
        return
    dst_dir = ctx.dst_root / "prompts"
    for f in sorted(src_dir.rglob("*.md")):
        rel = f.relative_to(src_dir)
        out_name = "agent-" + rel.as_posix().replace("/", "-")
        dst = dst_dir / out_name
        body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
        header = (
            f"<!-- Converted from Claude Code subagent: agents/{rel} -->\n"
            f"<!-- Original frontmatter:\n{fm}\n-->\n\n" if fm
            else f"<!-- Converted from Claude Code subagent: agents/{rel} -->\n\n"
        )
        _write_text(ctx, dst, header + body)
        ctx.report.moved.append(f"agents/{rel} → prompts/{out_name} (lossy)")


# ============================================================================
# MCP servers
# ============================================================================

def _normalize_mcp_claude_to_codex(name: str, spec: dict, report: Report) -> dict | None:
    """Claude shape:
        {"command": str, "args": [...], "env": {...}, "type": "stdio"|"sse"|"http", "url": str}
    Codex supports stdio MCP (command/args/env). SSE/HTTP gets skipped + reported.
    """
    t = (spec.get("type") or "stdio").lower()
    if t != "stdio":
        report.skipped.append(
            f"MCP server '{name}' uses type='{t}' (Codex only supports stdio)"
        )
        return None
    out: dict[str, Any] = {}
    if "command" in spec:
        out["command"] = spec["command"]
    if spec.get("args"):
        out["args"] = list(spec["args"])
    if spec.get("env"):
        out["env"] = dict(spec["env"])
    if not out.get("command"):
        report.skipped.append(f"MCP server '{name}' has no command — skipped")
        return None
    return out


def _normalize_mcp_codex_to_claude(name: str, spec: dict) -> dict:
    out: dict[str, Any] = {"type": "stdio"}
    if "command" in spec:
        out["command"] = spec["command"]
    if spec.get("args"):
        out["args"] = list(spec["args"])
    if spec.get("env"):
        out["env"] = dict(spec["env"])
    return out


# ============================================================================
# Settings.json <-> config.toml
# ============================================================================

def _load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except json.JSONDecodeError as e:
        print(f"warn: {p} is not valid JSON ({e}); treating as empty",
              file=sys.stderr)
        return {}


def _load_toml(p: Path) -> dict:
    try:
        return tomllib.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except tomllib.TOMLDecodeError as e:
        print(f"warn: {p} is not valid TOML ({e}); treating as empty",
              file=sys.stderr)
        return {}


def migrate_settings_claude_to_codex(ctx: Ctx) -> None:
    # Combine settings.json + settings.local.json (local wins).
    base = _load_json(ctx.src_root / "settings.json")
    local = _load_json(ctx.src_root / "settings.local.json")
    settings: dict = {**base, **local}
    if not settings:
        return

    dst = ctx.dst_root / "config.toml"
    existing = _load_toml(dst) if (ctx.merge and dst.exists()) else {}

    if "model" in settings:
        existing["model"] = settings["model"]
        ctx.report.moved.append("settings.json:model → config.toml:model")

    mcp = settings.get("mcpServers") or {}
    if mcp:
        existing.setdefault("mcp_servers", {})
        for name, spec in mcp.items():
            t = _normalize_mcp_claude_to_codex(name, spec, ctx.report)
            if t is not None:
                existing["mcp_servers"][name] = t
                ctx.report.moved.append(
                    f"mcpServers.{name} → [mcp_servers.{name}]"
                )

    for k in CLAUDE_UNMAPPABLE_KEYS:
        if k in settings:
            ctx.report.skipped.append(
                f"settings.json:{k} (no Codex equivalent)"
            )

    if existing:
        _write_text(ctx, dst, _render_toml_inmemory(existing))


def migrate_settings_codex_to_claude(ctx: Ctx) -> None:
    cfg = _load_toml(ctx.src_root / "config.toml")
    if not cfg:
        return

    dst = ctx.dst_root / "settings.json"
    existing = _load_json(dst) if (ctx.merge and dst.exists()) else {}

    if "model" in cfg:
        existing["model"] = cfg["model"]
        ctx.report.moved.append("config.toml:model → settings.json:model")

    mcp = cfg.get("mcp_servers") or {}
    if mcp:
        existing.setdefault("mcpServers", {})
        for name, spec in mcp.items():
            existing["mcpServers"][name] = _normalize_mcp_codex_to_claude(name, spec)
            ctx.report.moved.append(
                f"[mcp_servers.{name}] → mcpServers.{name}"
            )

    for k in CODEX_UNMAPPABLE_KEYS:
        if k in cfg:
            ctx.report.skipped.append(
                f"config.toml:{k} (no Claude Code equivalent)"
            )

    if existing:
        _write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")


def _render_toml_inmemory(data: dict) -> str:
    """Render TOML to string by writing to a temp Path-like buffer via write_toml."""
    # write_toml takes a Path; use a small wrapper.
    import io, os, tempfile
    fd, name = tempfile.mkstemp(suffix=".toml")
    os.close(fd)
    try:
        write_toml(data, Path(name))
        return Path(name).read_text(encoding="utf-8")
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


# ============================================================================
# Directory-level skip notes
# ============================================================================

def note_unmappable_dirs_claude(ctx: Ctx) -> None:
    for sub in ("agents", "skills", "plugins", "output-styles"):
        p = ctx.src_root / sub
        if p.is_dir() and any(p.iterdir()):
            if sub == "agents" and ctx.include_agents:
                continue
            ctx.report.skipped.append(f"{sub}/ — no Codex equivalent")


# ============================================================================
# Migration drivers
# ============================================================================

def run_claude_to_codex(ctx: Ctx) -> None:
    if not ctx.src_root.exists():
        print(f"Source not found: {ctx.src_root} — skipping.", file=sys.stderr)
        return
    if ctx.src_doc:
        migrate_doc(ctx, ctx.src_doc,
                    ctx.dst_doc or ctx.dst_root / "AGENTS.md",
                    f"{ctx.src_doc.name} → AGENTS.md")
    # Top-level user-style CLAUDE.md inside ~/.claude (rare but supported)
    migrate_doc(ctx, ctx.src_root / "CLAUDE.md",
                ctx.dst_root / "AGENTS.md",
                f"{ctx.src_root.name}/CLAUDE.md → {ctx.dst_root.name}/AGENTS.md")
    migrate_commands_to_prompts(ctx)
    migrate_agents_to_prompts(ctx)
    migrate_settings_claude_to_codex(ctx)
    note_unmappable_dirs_claude(ctx)


def run_codex_to_claude(ctx: Ctx) -> None:
    if not ctx.src_root.exists():
        print(f"Source not found: {ctx.src_root} — skipping.", file=sys.stderr)
        return
    if ctx.src_doc:
        migrate_doc(ctx, ctx.src_doc,
                    ctx.dst_doc or ctx.dst_root / "CLAUDE.md",
                    f"{ctx.src_doc.name} → CLAUDE.md")
    migrate_doc(ctx, ctx.src_root / "AGENTS.md",
                ctx.dst_root / "CLAUDE.md",
                f"{ctx.src_root.name}/AGENTS.md → {ctx.dst_root.name}/CLAUDE.md")
    migrate_prompts_to_commands(ctx)
    migrate_settings_codex_to_claude(ctx)


# ============================================================================
# CLI
# ============================================================================

def _resolve_paths(args: argparse.Namespace) -> list[tuple[Path, Path, Path | None, Path | None]]:
    """Return list of (claude_dir, codex_dir, claude_doc, codex_doc) pairs to process."""
    pairs: list[tuple[Path, Path, Path | None, Path | None]] = []

    if args.claude_dir or args.codex_dir:
        claude = Path(args.claude_dir).expanduser() if args.claude_dir else Path.home() / ".claude"
        codex = Path(args.codex_dir).expanduser() if args.codex_dir else Path.home() / ".codex"
        pairs.append((claude, codex, None, None))
        return pairs

    if args.scope in ("user", "both"):
        pairs.append((Path.home() / ".claude", Path.home() / ".codex", None, None))
    if args.scope in ("project", "both"):
        cwd = Path.cwd()
        pairs.append((
            cwd / ".claude", cwd / ".codex",
            cwd / "CLAUDE.md", cwd / "AGENTS.md",
        ))
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--direction", required=True,
                    choices=["claude-to-codex", "codex-to-claude"])
    ap.add_argument("--scope", choices=["user", "project", "both"],
                    default="user",
                    help="Which config scope to migrate (default: user).")
    ap.add_argument("--claude-dir", help="Explicit Claude config dir "
                    "(overrides --scope).")
    ap.add_argument("--codex-dir", help="Explicit Codex config dir "
                    "(overrides --scope).")
    ap.add_argument("--dry-run", action="store_true")
    merge_group = ap.add_mutually_exclusive_group()
    merge_group.add_argument("--merge", dest="merge", action="store_true",
                             default=True,
                             help="Merge into existing destination "
                             "(default).")
    merge_group.add_argument("--overwrite", dest="merge",
                             action="store_false",
                             help="Replace destination files.")
    ap.add_argument("--no-backup", dest="backup", action="store_false",
                    default=True)
    ap.add_argument("--include-agents", action="store_true",
                    help="Best-effort convert Claude subagents to Codex "
                    "prompts (lossy).")
    args = ap.parse_args()

    pairs = _resolve_paths(args)
    exit_code = 0

    for claude_dir, codex_dir, claude_doc, codex_doc in pairs:
        if args.direction == "claude-to-codex":
            src_root, dst_root = claude_dir, codex_dir
            src_doc, dst_doc = claude_doc, codex_doc
            label = f"Claude Code → Codex  ({src_root} → {dst_root})"
        else:
            src_root, dst_root = codex_dir, claude_dir
            src_doc, dst_doc = codex_doc, claude_doc
            label = f"Codex → Claude Code  ({src_root} → {dst_root})"

        if not src_root.exists() and not (src_doc and src_doc.exists()):
            print(f"\n--- {label}\n(no source files — nothing to do)")
            continue

        report = Report(direction=label)
        ctx = Ctx(
            src_root=src_root, dst_root=dst_root,
            src_doc=src_doc, dst_doc=dst_doc,
            dry_run=args.dry_run, merge=args.merge,
            backup=args.backup, include_agents=args.include_agents,
            report=report,
        )
        print(f"\n=== {label} ===")
        if args.direction == "claude-to-codex":
            run_claude_to_codex(ctx)
        else:
            run_codex_to_claude(ctx)

        if not args.dry_run and (report.moved or report.skipped):
            dst_root.mkdir(parents=True, exist_ok=True)
            (dst_root / "MIGRATION_REPORT.md").write_text(
                report.render(ctx.backup_root if args.backup else None),
                encoding="utf-8",
            )
        print(report.render(ctx.backup_root if args.backup else None))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
