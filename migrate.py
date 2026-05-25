#!/usr/bin/env python3
"""
migrate.py — Migrate settings + custom configuration between Claude Code
(~/.claude) and Codex CLI (~/.codex), in either direction.

Requires Python 3.11+ (uses tomllib).

Usage:
    python3 migrate.py --direction claude-to-codex
    python3 migrate.py --direction codex-to-claude
    # See --help for full options.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    print("This script requires Python 3.11+ (for tomllib).", file=sys.stderr)
    sys.exit(1)


# ============================================================================
# Constants
# ============================================================================

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
MIGRATOR_META_RE = re.compile(
    r"^<!--\s*migrator:meta\s+(?P<attrs>.+?)\s*-->\s*\n?", re.MULTILINE)
MIGRATOR_BEGIN = "<!-- migrator:begin kind={kind} source={source} -->"
MIGRATOR_END = "<!-- migrator:end -->"
MIGRATOR_BLOCK_RE = re.compile(
    r"<!--\s*migrator:begin\s+kind=(?P<kind>\S+)\s+source=(?P<source>\S+)\s*-->\n"
    r"(?P<body>.*?)\n?<!--\s*migrator:end\s*-->",
    re.DOTALL,
)

CLAUDE_UNMAPPABLE_KEYS = (
    "statusLine", "outputStyle",  # outputStyle handled as Tier A (content)
    "agentPushNotifEnabled", "remoteControlAtStartup",
    "autoUpdates", "verbose", "theme", "preferredNotifChannel",
    "enableAllProjectMcpServers", "enabledMcpjsonServers",
    "disabledMcpjsonServers", "alwaysThinkingEnabled",
)
CODEX_UNMAPPABLE_KEYS = (
    "model_provider", "model_providers", "tools", "tui",
    "hide_agent_reasoning", "show_raw_agent_reasoning",
    "model_reasoning_summary", "reasoning_summary", "model_verbosity",
    "disable_response_storage", "history", "project_doc_max_bytes",
)

EFFORT_C2X = {"low": "low", "medium": "medium", "high": "high", "max": "high"}
EFFORT_X2C = {"minimal": "low", "low": "low", "medium": "medium", "high": "high"}


# ============================================================================
# Tiny TOML writer
# ============================================================================

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


def _is_inline_dict(d: dict) -> bool:
    return all(not isinstance(v, dict) for v in d.values())


def render_toml(data: dict) -> str:
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}

    for k, v in scalars.items():
        lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
    if scalars and tables:
        lines.append("")

    def emit(prefix: str, table: dict) -> None:
        subtables = {k: v for k, v in table.items()
                     if isinstance(v, dict) and not _is_inline_dict(v)}
        own = {k: v for k, v in table.items() if k not in subtables}
        lines.append(f"[{prefix}]")
        for k, v in own.items():
            lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
        lines.append("")
        for sk, sv in subtables.items():
            emit(f"{prefix}.{_toml_key(sk)}", sv)

    for k, v in tables.items():
        emit(_toml_key(k), v)

    return "\n".join(lines).rstrip() + "\n"


def write_toml(data: dict, path: Path) -> None:
    path.write_text(render_toml(data), encoding="utf-8")


# ============================================================================
# Helpers
# ============================================================================

def ts() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def strip_frontmatter(text: str) -> tuple[str, dict | None]:
    """Return (body, parsed_frontmatter_or_None)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return text, None
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return text[m.end():], fm


def meta_comment_to_frontmatter(text: str) -> tuple[str, dict | None]:
    """Pull a top-level <!-- migrator:meta key="v" ... --> into a frontmatter dict."""
    m = MIGRATOR_META_RE.match(text)
    if not m:
        return text, None
    attrs = dict(re.findall(r'(\w[\w-]*)="((?:[^"\\]|\\.)*)"', m.group("attrs")))
    return text[m.end():], (attrs or None)


def frontmatter_to_meta_comment(fm: dict) -> str:
    keep = {k: v for k, v in fm.items() if k in ("description", "argument-hint")}
    if not keep:
        return ""
    parts = " ".join(f'{k}="{v}"' for k, v in keep.items())
    return f"<!-- migrator:meta {parts} -->\n"


def make_frontmatter(d: dict) -> str:
    lines = ["---"]
    for k, v in d.items():
        lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


def fenced_block(kind: str, source: str, body: str) -> str:
    body = body.rstrip()
    return (f"\n\n{MIGRATOR_BEGIN.format(kind=kind, source=source)}\n"
            f"{body}\n{MIGRATOR_END}\n")


def extract_fenced(text: str, kind: str) -> tuple[str, str | None]:
    """Return (text_with_block_removed, block_body_or_None) for first match of kind."""
    for m in MIGRATOR_BLOCK_RE.finditer(text):
        if m.group("kind") == kind:
            return text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip(), m.group("body")
    return text, None


# ============================================================================
# Report + I/O context
# ============================================================================

@dataclass
class Report:
    direction: str
    migrated_clean: list[str] = field(default_factory=list)
    migrated_lossy: list[str] = field(default_factory=list)
    skipped_by_user: list[str] = field(default_factory=list)
    skipped_unmappable: list[str] = field(default_factory=list)
    backups: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self, backup_root: Path | None) -> str:
        def section(title: str, items: list[str], empty: str = "_(none)_") -> list[str]:
            return [f"## {title}", *([f"- {x}" for x in items] if items else [empty]), ""]

        out = [
            f"# Migration report — {self.direction}",
            f"_{dt.datetime.now().isoformat(timespec='seconds')}_",
            "",
            *section("Migrated cleanly (Tier A)", self.migrated_clean),
            *section("Migrated with loss (Tier B, user-confirmed)", self.migrated_lossy),
            *section("Skipped by user choice", self.skipped_by_user),
            *section("Not translated (no equivalent exists)", self.skipped_unmappable),
        ]
        if self.notes:
            out += section("Notes", self.notes)
        out.append("## Backups")
        if backup_root and self.backups:
            out.append(f"Existing destination files moved to: `{backup_root}`")
            out += [f"- {x}" for x in self.backups]
        else:
            out.append("_(nothing replaced)_")
        return "\n".join(out) + "\n"


@dataclass
class Ctx:
    src_root: Path
    dst_root: Path
    src_doc: Path | None
    dst_doc: Path | None
    dry_run: bool
    merge: bool
    backup: bool
    report: Report
    backup_root: Path = field(init=False)

    def __post_init__(self) -> None:
        self.backup_root = self.dst_root / "backups" / f"pre-migrate-{ts()}"


def _backup_if_exists(ctx: Ctx, path: Path) -> None:
    if not path.exists() or not ctx.backup:
        return
    dest = ctx.backup_root / path.name
    i = 1
    while dest.exists():
        dest = ctx.backup_root / f"{path.stem}.{i}{path.suffix}"
        i += 1
    if ctx.dry_run:
        ctx.report.backups.append(f"would back up: {path} → {dest}")
        return
    ctx.backup_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    ctx.report.backups.append(str(dest))


def write_text(ctx: Ctx, path: Path, content: str) -> None:
    _backup_if_exists(ctx, path)
    if ctx.dry_run:
        print(f"[dry-run] write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def copy_file(ctx: Ctx, src: Path, dst: Path) -> None:
    _backup_if_exists(ctx, dst)
    if ctx.dry_run:
        print(f"[dry-run] copy {src} → {dst}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def load_json(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"warn: {p} not valid JSON ({e}); treating as empty", file=sys.stderr)
        return {}


def load_toml(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        print(f"warn: {p} not valid TOML ({e}); treating as empty", file=sys.stderr)
        return {}


def load_claude_settings(claude_dir: Path) -> dict:
    base = load_json(claude_dir / "settings.json")
    local = load_json(claude_dir / "settings.local.json")
    return {**base, **local}


# ============================================================================
# Tier A — clean translations (always applied)
# ============================================================================

def tier_a_docs_claude_to_codex(ctx: Ctx) -> None:
    """CLAUDE.md → AGENTS.md, with optional outputStyle content fenced in."""
    src = ctx.src_doc or (ctx.src_root / "CLAUDE.md")
    if not src.exists():
        src = ctx.src_root / "CLAUDE.md"
    dst = ctx.dst_doc or (ctx.dst_root / "AGENTS.md")

    content_parts: list[str] = []
    if src.exists():
        content_parts.append(src.read_text(encoding="utf-8").rstrip() + "\n")

    # outputStyle: if set, append the style file's content fenced.
    settings = load_claude_settings(ctx.src_root)
    style_name = settings.get("outputStyle")
    if style_name:
        for cand in [ctx.src_root / "output-styles" / f"{style_name}.md",
                     Path.home() / ".claude" / "output-styles" / f"{style_name}.md"]:
            if cand.exists():
                content_parts.append(fenced_block(
                    "outputStyle", style_name,
                    cand.read_text(encoding="utf-8")))
                ctx.report.migrated_clean.append(
                    f"outputStyle '{style_name}' → fenced block in AGENTS.md")
                break

    if not content_parts:
        return

    body = "".join(content_parts)
    if dst.exists() and ctx.merge:
        existing = dst.read_text(encoding="utf-8").rstrip()
        body = existing + "\n\n" + body if existing else body
    write_text(ctx, dst, body)
    if src.exists():
        ctx.report.migrated_clean.append(f"{src.name} → {dst.name}")


def tier_a_docs_codex_to_claude(ctx: Ctx) -> None:
    """AGENTS.md → CLAUDE.md, plus config.toml:instructions appended fenced."""
    src = ctx.src_doc or (ctx.src_root / "AGENTS.md")
    if not src.exists():
        src = ctx.src_root / "AGENTS.md"
    dst = ctx.dst_doc or (ctx.dst_root / "CLAUDE.md")

    content_parts: list[str] = []
    if src.exists():
        content_parts.append(src.read_text(encoding="utf-8").rstrip() + "\n")

    cfg = load_toml(ctx.src_root / "config.toml")
    if "instructions" in cfg and isinstance(cfg["instructions"], str):
        content_parts.append(fenced_block(
            "instructions", "config.toml", cfg["instructions"]))
        ctx.report.migrated_clean.append(
            "config.toml:instructions → fenced block in CLAUDE.md")

    if not content_parts:
        return

    body = "".join(content_parts)
    if dst.exists() and ctx.merge:
        existing = dst.read_text(encoding="utf-8").rstrip()
        body = existing + "\n\n" + body if existing else body
    write_text(ctx, dst, body)
    if src.exists():
        ctx.report.migrated_clean.append(f"{src.name} → {dst.name}")


def tier_a_commands_to_prompts(ctx: Ctx) -> None:
    src_dir = ctx.src_root / "commands"
    if not src_dir.is_dir():
        return
    dst_dir = ctx.dst_root / "prompts"
    for f in sorted(src_dir.rglob("*.md")):
        rel = f.relative_to(src_dir)
        body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
        meta = frontmatter_to_meta_comment(fm or {})
        dropped = sorted((fm or {}).keys() - {"description", "argument-hint"})
        if dropped:
            ctx.report.notes.append(
                f"commands/{rel}: dropped frontmatter keys: {', '.join(dropped)}")
        write_text(ctx, dst_dir / rel, meta + body)
        ctx.report.migrated_clean.append(f"commands/{rel} → prompts/{rel}")


def tier_a_prompts_to_commands(ctx: Ctx) -> None:
    src_dir = ctx.src_root / "prompts"
    if not src_dir.is_dir():
        return
    dst_dir = ctx.dst_root / "commands"
    for f in sorted(src_dir.rglob("*.md")):
        rel = f.relative_to(src_dir)
        text = f.read_text(encoding="utf-8")
        body, meta = meta_comment_to_frontmatter(text)
        if meta:
            body = make_frontmatter(meta) + body
        write_text(ctx, dst_dir / rel, body)
        ctx.report.migrated_clean.append(f"prompts/{rel} → commands/{rel}")


def _normalize_mcp_claude_to_codex(name: str, spec: dict, report: Report) -> dict | None:
    t = (spec.get("type") or "stdio").lower()
    if t != "stdio":
        report.skipped_unmappable.append(
            f"MCP server '{name}' uses type='{t}' (Codex only supports stdio)")
        return None
    out: dict[str, Any] = {}
    if "command" in spec:
        out["command"] = spec["command"]
    if spec.get("args"):
        out["args"] = list(spec["args"])
    if spec.get("env"):
        out["env"] = dict(spec["env"])
    if not out.get("command"):
        report.skipped_unmappable.append(f"MCP server '{name}' has no command")
        return None
    return out


def _normalize_mcp_codex_to_claude(spec: dict) -> dict:
    out: dict[str, Any] = {"type": "stdio"}
    for k in ("command", "args", "env"):
        if spec.get(k):
            out[k] = spec[k] if k == "command" else (
                list(spec[k]) if k == "args" else dict(spec[k]))
    return out


def tier_a_settings_claude_to_codex(ctx: Ctx) -> None:
    settings = load_claude_settings(ctx.src_root)
    if not settings:
        return
    dst = ctx.dst_root / "config.toml"
    existing = load_toml(dst) if (ctx.merge and dst.exists()) else {}

    if "model" in settings:
        existing["model"] = settings["model"]
        ctx.report.migrated_clean.append("settings.json:model → config.toml:model")

    mcp = settings.get("mcpServers") or {}
    if mcp:
        existing.setdefault("mcp_servers", {})
        for name, spec in mcp.items():
            t = _normalize_mcp_claude_to_codex(name, spec, ctx.report)
            if t is not None:
                existing["mcp_servers"][name] = t
                ctx.report.migrated_clean.append(
                    f"mcpServers.{name} → [mcp_servers.{name}]")

    if isinstance(settings.get("env"), dict) and settings["env"]:
        existing.setdefault("shell_environment_policy", {})
        existing["shell_environment_policy"].setdefault("set", {})
        existing["shell_environment_policy"]["set"].update(settings["env"])
        ctx.report.migrated_clean.append(
            f"settings.json:env ({len(settings['env'])} vars) → "
            "[shell_environment_policy] set")

    if "effortLevel" in settings:
        mapped = EFFORT_C2X.get(settings["effortLevel"])
        if mapped:
            existing["model_reasoning_effort"] = mapped
            ctx.report.migrated_clean.append(
                f"effortLevel={settings['effortLevel']} → "
                f"model_reasoning_effort={mapped}")

    for k in CLAUDE_UNMAPPABLE_KEYS:
        if k in settings:
            ctx.report.skipped_unmappable.append(
                f"settings.json:{k} (no Codex equivalent)")

    if existing:
        write_text(ctx, dst, render_toml(existing))


def tier_a_settings_codex_to_claude(ctx: Ctx) -> None:
    cfg = load_toml(ctx.src_root / "config.toml")
    if not cfg:
        return
    dst = ctx.dst_root / "settings.json"
    existing = load_json(dst) if (ctx.merge and dst.exists()) else {}

    if "model" in cfg:
        existing["model"] = cfg["model"]
        ctx.report.migrated_clean.append("config.toml:model → settings.json:model")

    mcp = cfg.get("mcp_servers") or {}
    if mcp:
        existing.setdefault("mcpServers", {})
        for name, spec in mcp.items():
            existing["mcpServers"][name] = _normalize_mcp_codex_to_claude(spec)
            ctx.report.migrated_clean.append(
                f"[mcp_servers.{name}] → mcpServers.{name}")

    sep = cfg.get("shell_environment_policy") or {}
    set_vars = sep.get("set") or {}
    if set_vars:
        existing.setdefault("env", {})
        existing["env"].update({k: str(v) for k, v in set_vars.items()})
        ctx.report.migrated_clean.append(
            f"[shell_environment_policy] set ({len(set_vars)} vars) → settings.json:env")
    for k in ("include_only", "exclude", "inherit"):
        if k in sep:
            ctx.report.skipped_unmappable.append(
                f"shell_environment_policy.{k} (no Claude Code equivalent)")

    eff = cfg.get("model_reasoning_effort")
    if isinstance(eff, str):
        mapped = EFFORT_X2C.get(eff)
        if mapped:
            existing["effortLevel"] = mapped
            ctx.report.migrated_clean.append(
                f"model_reasoning_effort={eff} → effortLevel={mapped}")

    for k in CODEX_UNMAPPABLE_KEYS:
        if k in cfg:
            ctx.report.skipped_unmappable.append(
                f"config.toml:{k} (no Claude Code equivalent)")

    if existing:
        write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")


# ============================================================================
# Tier B — lossy translations (user-confirmed)
# ============================================================================

@dataclass
class LossyOption:
    id: str
    direction: str  # 'c2x' or 'x2c'
    label: str
    rationale: str
    detect: Callable[[Ctx], bool]
    preview: Callable[[Ctx], str]
    apply: Callable[[Ctx], None]


# ---- B1: permissions ↔ sandbox + approval ---------------------------------

def _detect_claude_permissions(ctx: Ctx) -> bool:
    s = load_claude_settings(ctx.src_root)
    return bool(s.get("permissions"))


def _preview_claude_permissions(ctx: Ctx) -> str:
    s = load_claude_settings(ctx.src_root).get("permissions", {})
    parts = []
    for key in ("allow", "deny", "ask"):
        if s.get(key):
            parts.append(f"{key}={len(s[key])} rules")
    return "permissions → sandbox_mode + approval_policy (heuristic). " + ", ".join(parts)


def _apply_claude_permissions(ctx: Ctx) -> None:
    s = load_claude_settings(ctx.src_root).get("permissions", {}) or {}
    allow = s.get("allow") or []
    deny = s.get("deny") or []

    dst = ctx.dst_root / "config.toml"
    existing = load_toml(dst) if dst.exists() else {}

    has_bash_all = any(re.fullmatch(r"Bash\(\*\)?|.*\*.*", a) and "Bash" in a for a in allow)
    only_reads = allow and all(a.startswith("Read(") for a in allow)
    write_patterns = [a for a in allow if a.startswith("Write(")]
    deny_net = any(re.search(r"Web(Fetch|Search)", d) for d in deny)

    if has_bash_all and not deny:
        existing["approval_policy"] = "never"
        existing["sandbox_mode"] = "danger-full-access"
        explain = "Bash(*) in allow + no deny → danger-full-access / approval=never"
    elif only_reads:
        existing["sandbox_mode"] = "read-only"
        existing["approval_policy"] = "on-request"
        explain = "Only Read() patterns allowed → sandbox=read-only / approval=on-request"
    else:
        existing["sandbox_mode"] = "workspace-write"
        existing["approval_policy"] = "on-request"
        explain = "Mixed rules → sandbox=workspace-write / approval=on-request"

    if write_patterns:
        roots: list[str] = []
        for w in write_patterns:
            m = re.match(r"Write\((.+?)\)", w)
            if m:
                p = re.sub(r"/?\*\*?$", "", m.group(1)).rstrip("/")
                if p and p not in roots:
                    roots.append(p)
        if roots:
            existing.setdefault("sandbox_workspace_write", {})
            existing["sandbox_workspace_write"]["writable_roots"] = roots
    if deny_net:
        existing.setdefault("sandbox_workspace_write", {})
        existing["sandbox_workspace_write"]["network_access"] = False

    write_text(ctx, dst, render_toml(existing))
    ctx.report.migrated_lossy.append(
        f"permissions → sandbox_mode/approval_policy ({explain})")
    if s.get("ask"):
        ctx.report.notes.append(
            f"permissions.ask ({len(s['ask'])} rules) has no Codex equivalent — "
            "covered loosely by approval_policy=on-request")


def _detect_codex_sandbox(ctx: Ctx) -> bool:
    c = load_toml(ctx.src_root / "config.toml")
    return any(k in c for k in
               ("sandbox_mode", "approval_policy", "sandbox_workspace_write"))


def _preview_codex_sandbox(ctx: Ctx) -> str:
    c = load_toml(ctx.src_root / "config.toml")
    parts = []
    if "sandbox_mode" in c:
        parts.append(f"sandbox_mode={c['sandbox_mode']}")
    if "approval_policy" in c:
        parts.append(f"approval_policy={c['approval_policy']}")
    return "sandbox/approval → permissions (heuristic). " + ", ".join(parts)


def _apply_codex_sandbox(ctx: Ctx) -> None:
    c = load_toml(ctx.src_root / "config.toml")
    dst = ctx.dst_root / "settings.json"
    existing = load_json(dst) if dst.exists() else {}
    perms = existing.setdefault("permissions", {})
    allow = list(perms.get("allow") or [])
    deny = list(perms.get("deny") or [])

    mode = c.get("sandbox_mode", "workspace-write")
    if mode == "danger-full-access":
        allow = ["Bash(*)", "Read(*)", "Write(*)", "WebFetch(*)"]
    elif mode == "read-only":
        allow = ["Read(*)"]
        deny += ["Write(*)", "Bash(*)"]
    else:  # workspace-write
        allow = ["Read(*)", "Bash(*)"]
        sww = c.get("sandbox_workspace_write") or {}
        roots = sww.get("writable_roots") or ["."]
        for r in roots:
            allow.append(f"Write({r.rstrip('/')}/**)")
        if sww.get("network_access") is False:
            deny += ["WebFetch(*)"]

    perms["allow"] = sorted(set(allow))
    if deny:
        perms["deny"] = sorted(set(deny))

    write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")
    ctx.report.migrated_lossy.append(
        f"sandbox_mode={mode!r} + approval_policy={c.get('approval_policy','?')!r} "
        "→ permissions.allow/deny (coarse)")


# ---- B2: hooks ↔ notify ----------------------------------------------------

def _detect_claude_notify_hook(ctx: Ctx) -> bool:
    s = load_claude_settings(ctx.src_root)
    hooks = s.get("hooks") or {}
    return any(k in hooks for k in ("Notification", "Stop"))


def _preview_claude_notify_hook(ctx: Ctx) -> str:
    s = load_claude_settings(ctx.src_root)
    hooks = s.get("hooks") or {}
    kinds = [k for k in ("Notification", "Stop") if k in hooks]
    other = [k for k in hooks if k not in ("Notification", "Stop")]
    msg = f"hooks: {', '.join(kinds)} → notify"
    if other:
        msg += f". DROPPED hook types: {', '.join(other)}"
    return msg


def _extract_first_command(entries: list) -> list[str] | None:
    """Claude hook shape: [{matcher, hooks: [{type:'command', command:'...'}]}]."""
    for grp in entries or []:
        for h in (grp.get("hooks") or []) if isinstance(grp, dict) else []:
            if h.get("type") == "command" and h.get("command"):
                cmd = h["command"]
                # Codex notify wants a list (argv-style). Best effort split.
                return ["/bin/sh", "-c", cmd]
    return None


def _apply_claude_notify_hook(ctx: Ctx) -> None:
    s = load_claude_settings(ctx.src_root)
    hooks = s.get("hooks") or {}
    cmd = _extract_first_command(hooks.get("Notification") or hooks.get("Stop"))
    if not cmd:
        return
    dst = ctx.dst_root / "config.toml"
    existing = load_toml(dst) if dst.exists() else {}
    existing["notify"] = cmd
    write_text(ctx, dst, render_toml(existing))
    ctx.report.migrated_lossy.append(
        "hooks.Notification/Stop → config.toml:notify "
        "(wrapped via /bin/sh -c; matcher patterns dropped)")
    other = [k for k in hooks if k not in ("Notification", "Stop")]
    for k in other:
        ctx.report.skipped_unmappable.append(
            f"hooks.{k} (no Codex equivalent — PreToolUse/PostToolUse/etc. are Claude-only)")


def _detect_codex_notify(ctx: Ctx) -> bool:
    c = load_toml(ctx.src_root / "config.toml")
    return bool(c.get("notify"))


def _preview_codex_notify(ctx: Ctx) -> str:
    c = load_toml(ctx.src_root / "config.toml")
    return f"notify={c.get('notify')} → hooks.Notification"


def _apply_codex_notify(ctx: Ctx) -> None:
    c = load_toml(ctx.src_root / "config.toml")
    cmd = c.get("notify")
    if not cmd:
        return
    if isinstance(cmd, list):
        cmd_str = " ".join(cmd)
    else:
        cmd_str = str(cmd)
    dst = ctx.dst_root / "settings.json"
    existing = load_json(dst) if dst.exists() else {}
    existing.setdefault("hooks", {}).setdefault("Notification", []).append({
        "hooks": [{"type": "command", "command": cmd_str}],
    })
    write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")
    ctx.report.migrated_lossy.append(
        "config.toml:notify → hooks.Notification (single command, no matcher)")


# ---- B3: agents → prompts (one-way) ---------------------------------------

def _detect_claude_agents(ctx: Ctx) -> bool:
    p = ctx.src_root / "agents"
    return p.is_dir() and any(p.rglob("*.md"))


def _preview_claude_agents(ctx: Ctx) -> str:
    n = sum(1 for _ in (ctx.src_root / "agents").rglob("*.md"))
    return f"{n} subagent file(s) → prompts/agent-*.md (flattened; loses subagent semantics)"


def _apply_claude_agents(ctx: Ctx) -> None:
    src_dir = ctx.src_root / "agents"
    dst_dir = ctx.dst_root / "prompts"
    for f in sorted(src_dir.rglob("*.md")):
        rel = f.relative_to(src_dir)
        out_name = "agent-" + rel.as_posix().replace("/", "-")
        body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
        header = f"<!-- Converted from Claude subagent: agents/{rel} -->\n"
        if fm:
            header += (f"<!-- Original frontmatter: "
                       f"{json.dumps(fm, ensure_ascii=False)} -->\n")
        header += "\n"
        write_text(ctx, dst_dir / out_name, header + body)
        ctx.report.migrated_lossy.append(
            f"agents/{rel} → prompts/{out_name} (lossy: no subagent runtime)")


# ---- B4: skills → prompts (one-way) ---------------------------------------

def _detect_claude_skills(ctx: Ctx) -> bool:
    p = ctx.src_root / "skills"
    return p.is_dir() and any(p.glob("*/SKILL.md"))


def _preview_claude_skills(ctx: Ctx) -> str:
    n = sum(1 for _ in (ctx.src_root / "skills").glob("*/SKILL.md"))
    return f"{n} skill(s) → prompts/skill-*.md (flattened; loses skill-discovery + assets)"


def _apply_claude_skills(ctx: Ctx) -> None:
    src_dir = ctx.src_root / "skills"
    dst_dir = ctx.dst_root / "prompts"
    for skill_md in sorted(src_dir.glob("*/SKILL.md")):
        skill_name = skill_md.parent.name
        out_name = f"skill-{skill_name}.md"
        body, fm = strip_frontmatter(skill_md.read_text(encoding="utf-8"))
        header = f"<!-- Converted from Claude skill: skills/{skill_name}/ -->\n"
        if fm:
            header += (f"<!-- Original frontmatter: "
                       f"{json.dumps(fm, ensure_ascii=False)} -->\n")
        # Note any non-SKILL.md assets in the skill dir.
        assets = [p for p in skill_md.parent.rglob("*")
                  if p.is_file() and p.name != "SKILL.md"]
        if assets:
            header += (f"<!-- Skill bundled {len(assets)} asset file(s); "
                       "not migrated. See original skill dir. -->\n")
        header += "\n"
        write_text(ctx, dst_dir / out_name, header + body)
        ctx.report.migrated_lossy.append(
            f"skills/{skill_name}/ → prompts/{out_name} "
            f"(lossy: assets not migrated)")


# ---- B5: codex profiles → ~/.claude/profiles/*.json -----------------------

def _detect_codex_profiles(ctx: Ctx) -> bool:
    c = load_toml(ctx.src_root / "config.toml")
    return bool(c.get("profiles"))


def _preview_codex_profiles(ctx: Ctx) -> str:
    c = load_toml(ctx.src_root / "config.toml")
    names = list((c.get("profiles") or {}).keys())
    return (f"{len(names)} Codex profile(s): {', '.join(names)} → "
            "~/.claude/profiles/*.settings.json (no Claude profile runtime; "
            "swap manually)")


def _apply_codex_profiles(ctx: Ctx) -> None:
    c = load_toml(ctx.src_root / "config.toml")
    base = {k: v for k, v in c.items() if k != "profiles"}
    profiles = c.get("profiles") or {}
    out_dir = ctx.dst_root / "profiles"
    for name, override in profiles.items():
        merged = {**base, **(override if isinstance(override, dict) else {})}
        # Translate merged config into Claude settings shape using a temp ctx.
        sub_report = Report(direction=f"profile:{name}")
        # Build a transient cfg file scenario inline:
        out: dict = {}
        if "model" in merged:
            out["model"] = merged["model"]
        if merged.get("mcp_servers"):
            out["mcpServers"] = {
                n: _normalize_mcp_codex_to_claude(spec)
                for n, spec in merged["mcp_servers"].items()
            }
        sep = merged.get("shell_environment_policy") or {}
        if sep.get("set"):
            out["env"] = {k: str(v) for k, v in sep["set"].items()}
        if isinstance(merged.get("model_reasoning_effort"), str):
            m = EFFORT_X2C.get(merged["model_reasoning_effort"])
            if m:
                out["effortLevel"] = m
        path = out_dir / f"{name}.settings.json"
        write_text(ctx, path, json.dumps(out, indent=2) + "\n")
        ctx.report.migrated_lossy.append(
            f"[profiles.{name}] → {path.relative_to(ctx.dst_root)} "
            "(no Claude profile runtime — copy over settings.json to activate)")
        _ = sub_report  # unused but kept to mirror structure


# ---- Catalog ---------------------------------------------------------------

TIER_B: list[LossyOption] = [
    LossyOption(
        id="permissions",
        direction="c2x",
        label="permissions → sandbox_mode + approval_policy",
        rationale=("Claude's per-tool regex permissions don't map exactly to "
                   "Codex's coarse sandbox modes. We infer the closest match."),
        detect=_detect_claude_permissions,
        preview=_preview_claude_permissions,
        apply=_apply_claude_permissions,
    ),
    LossyOption(
        id="sandbox",
        direction="x2c",
        label="sandbox_mode/approval_policy → permissions",
        rationale=("Codex's coarse sandbox mode is expanded into a set of "
                   "Claude allow/deny patterns. Round-trip is not exact."),
        detect=_detect_codex_sandbox,
        preview=_preview_codex_sandbox,
        apply=_apply_codex_sandbox,
    ),
    LossyOption(
        id="hooks",
        direction="c2x",
        label="hooks.Notification/Stop → notify",
        rationale=("Codex `notify` covers a subset of Claude's hook events. "
                   "Other hook types (PreToolUse, etc.) are dropped."),
        detect=_detect_claude_notify_hook,
        preview=_preview_claude_notify_hook,
        apply=_apply_claude_notify_hook,
    ),
    LossyOption(
        id="notify",
        direction="x2c",
        label="notify → hooks.Notification",
        rationale=("Codex's single notify program is registered as a Claude "
                   "Notification hook without a matcher."),
        detect=_detect_codex_notify,
        preview=_preview_codex_notify,
        apply=_apply_codex_notify,
    ),
    LossyOption(
        id="agents",
        direction="c2x",
        label="agents/ → prompts/agent-*.md",
        rationale=("Codex has no subagent system. Each agent.md is flattened "
                   "into a plain prompt; the subagent runtime is lost."),
        detect=_detect_claude_agents,
        preview=_preview_claude_agents,
        apply=_apply_claude_agents,
    ),
    LossyOption(
        id="skills",
        direction="c2x",
        label="skills/ → prompts/skill-*.md",
        rationale=("Codex has no skills system. SKILL.md becomes a flat prompt; "
                   "bundled assets are not migrated and skill auto-discovery is lost."),
        detect=_detect_claude_skills,
        preview=_preview_claude_skills,
        apply=_apply_claude_skills,
    ),
    LossyOption(
        id="profiles",
        direction="x2c",
        label="[profiles.*] → ~/.claude/profiles/*.settings.json",
        rationale=("Claude has no profile runtime. Each Codex profile is "
                   "materialized as a standalone settings file you can copy "
                   "over settings.json to activate."),
        detect=_detect_codex_profiles,
        preview=_preview_codex_profiles,
        apply=_apply_codex_profiles,
    ),
]


# ============================================================================
# Pre-flight interactive scan
# ============================================================================

def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def ask_yn(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"{prompt} {suffix}: ").strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans.startswith("y")


def preflight(ctx: Ctx, direction_key: str,
              apply_set: set[str] | None,
              skip_set: set[str] | None,
              interactive: bool) -> dict[str, bool]:
    """Return {option_id: should_apply} for Tier B options applicable here."""
    candidates = [o for o in TIER_B if o.direction == direction_key and o.detect(ctx)]
    decisions: dict[str, bool] = {}

    print()
    print("=" * 72)
    print(f"Pre-migration scan — {ctx.report.direction}")
    print("=" * 72)
    print(f"Source: {ctx.src_root}")
    print(f"Destination: {ctx.dst_root}")
    print()

    # Tier A summary (just announce what will run).
    print("Tier A — clean translations (always applied):")
    a_items = []
    if (ctx.src_root / "CLAUDE.md").exists() or (ctx.src_root / "AGENTS.md").exists() \
            or (ctx.src_doc and ctx.src_doc.exists()):
        a_items.append("instruction doc (CLAUDE.md ↔ AGENTS.md)")
    if (ctx.src_root / "commands").is_dir() or (ctx.src_root / "prompts").is_dir():
        a_items.append("slash commands ↔ prompts")
    if (ctx.src_root / "settings.json").exists() or (ctx.src_root / "config.toml").exists():
        a_items.append("model + mcpServers + env + reasoning effort")
    for x in a_items:
        print(f"  ✓ {x}")
    if not a_items:
        print("  (nothing detected)")
    print()

    if not candidates:
        print("Tier B — lossy translations: none detected.")
        print()
    else:
        print("Tier B — lossy translations (please confirm):")
        for i, opt in enumerate(candidates, 1):
            forced_apply = apply_set and (opt.id in apply_set or "all" in apply_set)
            forced_skip = skip_set and (opt.id in skip_set or "all" in skip_set)
            print(f"  [{i}] {opt.label}")
            print(f"      why lossy: {opt.rationale}")
            print(f"      preview:   {opt.preview(ctx)}")
            if forced_apply:
                decisions[opt.id] = True
                print("      → APPLY (forced via --apply-lossy)")
            elif forced_skip:
                decisions[opt.id] = False
                print("      → SKIP (forced via --skip-lossy)")
            elif interactive:
                decisions[opt.id] = ask_yn("      Apply this translation?", True)
            else:
                decisions[opt.id] = True
                print("      → APPLY (non-interactive default)")
            print()

    # Tier C preview (best-effort; full details land in the report after run).
    print("Tier C — items with no equivalent will be listed in MIGRATION_REPORT.md.")
    print()

    if interactive and not ask_yn("Proceed with migration?", True):
        print("Aborted by user.")
        sys.exit(0)

    return decisions


# ============================================================================
# Drivers
# ============================================================================

def run_claude_to_codex(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    tier_a_docs_claude_to_codex(ctx)
    tier_a_commands_to_prompts(ctx)
    tier_a_settings_claude_to_codex(ctx)

    for opt in TIER_B:
        if opt.direction != "c2x" or not opt.detect(ctx):
            continue
        if lossy_decisions.get(opt.id):
            opt.apply(ctx)
        else:
            ctx.report.skipped_by_user.append(f"{opt.label} (declined)")

    # Items declared unmappable but not yet noted via settings pass.
    for sub in ("plugins",):
        p = ctx.src_root / sub
        if p.is_dir() and any(p.iterdir()):
            ctx.report.skipped_unmappable.append(f"{sub}/ (Claude-only runtime)")


def run_codex_to_claude(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    tier_a_docs_codex_to_claude(ctx)
    tier_a_prompts_to_commands(ctx)
    tier_a_settings_codex_to_claude(ctx)

    for opt in TIER_B:
        if opt.direction != "x2c" or not opt.detect(ctx):
            continue
        if lossy_decisions.get(opt.id):
            opt.apply(ctx)
        else:
            ctx.report.skipped_by_user.append(f"{opt.label} (declined)")


# ============================================================================
# CLI
# ============================================================================

def _resolve_pairs(args: argparse.Namespace) -> list[tuple[Path, Path, Path | None, Path | None]]:
    if args.claude_dir or args.codex_dir:
        claude = Path(args.claude_dir).expanduser() if args.claude_dir else Path.home() / ".claude"
        codex = Path(args.codex_dir).expanduser() if args.codex_dir else Path.home() / ".codex"
        return [(claude, codex, None, None)]
    pairs: list[tuple[Path, Path, Path | None, Path | None]] = []
    if args.scope in ("user", "both"):
        pairs.append((Path.home() / ".claude", Path.home() / ".codex", None, None))
    if args.scope in ("project", "both"):
        cwd = Path.cwd()
        pairs.append((cwd / ".claude", cwd / ".codex",
                      cwd / "CLAUDE.md", cwd / "AGENTS.md"))
    return pairs


def _csv_set(s: str | None) -> set[str] | None:
    if not s:
        return None
    return {x.strip() for x in s.split(",") if x.strip()}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--direction", required=True,
                    choices=["claude-to-codex", "codex-to-claude"])
    ap.add_argument("--scope", choices=["user", "project", "both"],
                    default="user")
    ap.add_argument("--claude-dir")
    ap.add_argument("--codex-dir")
    ap.add_argument("--dry-run", action="store_true")
    mg = ap.add_mutually_exclusive_group()
    mg.add_argument("--merge", dest="merge", action="store_true", default=True)
    mg.add_argument("--overwrite", dest="merge", action="store_false")
    ap.add_argument("--no-backup", dest="backup", action="store_false", default=True)
    ap.add_argument("--no-interactive", action="store_true",
                    help="Don't prompt; use --apply-lossy / --skip-lossy "
                    "(default: apply all detected lossy translations).")
    ap.add_argument("--apply-lossy", metavar="IDS",
                    help="Comma-separated Tier B IDs to apply unconditionally "
                    "(or 'all'). IDs: " + ", ".join(o.id for o in TIER_B))
    ap.add_argument("--skip-lossy", metavar="IDS",
                    help="Comma-separated Tier B IDs to skip unconditionally "
                    "(or 'all').")
    args = ap.parse_args()

    apply_set = _csv_set(args.apply_lossy)
    skip_set = _csv_set(args.skip_lossy)
    interactive = not args.no_interactive and is_interactive() \
        and not (apply_set or skip_set)

    direction_key = "c2x" if args.direction == "claude-to-codex" else "x2c"
    pairs = _resolve_pairs(args)

    for claude_dir, codex_dir, claude_doc, codex_doc in pairs:
        if direction_key == "c2x":
            src_root, dst_root = claude_dir, codex_dir
            src_doc, dst_doc = claude_doc, codex_doc
        else:
            src_root, dst_root = codex_dir, claude_dir
            src_doc, dst_doc = codex_doc, claude_doc

        label = (f"{'Claude Code → Codex' if direction_key == 'c2x' else 'Codex → Claude Code'}"
                 f"  ({src_root} → {dst_root})")

        if not src_root.exists() and not (src_doc and src_doc.exists()):
            print(f"\n--- {label}\n(no source files — skipped)")
            continue

        report = Report(direction=label)
        ctx = Ctx(src_root=src_root, dst_root=dst_root,
                  src_doc=src_doc, dst_doc=dst_doc,
                  dry_run=args.dry_run, merge=args.merge,
                  backup=args.backup, report=report)

        decisions = preflight(ctx, direction_key, apply_set, skip_set, interactive)

        print(f"\n--- Running: {label}")
        if direction_key == "c2x":
            run_claude_to_codex(ctx, decisions)
        else:
            run_codex_to_claude(ctx, decisions)

        if not args.dry_run:
            dst_root.mkdir(parents=True, exist_ok=True)
            (dst_root / "MIGRATION_REPORT.md").write_text(
                report.render(ctx.backup_root if args.backup else None),
                encoding="utf-8")
        print()
        print(report.render(ctx.backup_root if args.backup else None))

    return 0


if __name__ == "__main__":
    sys.exit(main())
