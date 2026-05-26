#!/usr/bin/env python3
"""
migrate.py — Migrate settings + custom configuration between Claude Code
(~/.claude) and Codex CLI (~/.codex), in either direction.

Requires Python 3.9+. No third-party dependencies.

Usage:
    python3 migrate.py --direction claude-to-codex
    python3 migrate.py --direction codex-to-claude
    python3 migrate.py --restore [BACKUP_DIR]      # revert a previous run
    # See --help for full options.

Design overview
---------------
Migration runs in five phases:

  1. **Preflight scan**  — detect Tier B (lossy) translations and let the
     user accept/skip each one. Tier A (clean) items are always applied.
  2. **Plan pass**       — re-run the migration with `Ctx.plan_mode=True`;
     write helpers record destination paths into `Ctx.planned_writes`
     instead of touching the filesystem. This produces a complete list of
     destination files *before* any writes happen.
  3. **Backup**          — copy every planned path that already exists into
     `<dst>/backups/pre-migrate-<timestamp>/`, preserving relative layout,
     and write a `manifest.json` recording each entry's `existed_before`
     flag. `--restore` later uses this to reverse the migration.
  4. **Confirm**         — show the user the planned changes and ask one
     final time before any writes.
  5. **Apply pass**      — run the migration again with `plan_mode=False`;
     write helpers actually write. Each function re-reads the destination
     from disk, so layered writes (e.g., Tier A then Tier B both touching
     settings.json) compose correctly.

The round-trippable pieces of content (Codex `instructions`, Claude
`outputStyle` files, slash-command frontmatter `description`/
`argument-hint`) ride along inside HTML comments that the reverse
direction recognizes and unwraps. See the `fenced_block` / `extract_fenced`
and `frontmatter_to_meta_comment` / `meta_comment_to_frontmatter` helpers.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# ----------------------------------------------------------------------------
# TOML reader compatibility shim
# ----------------------------------------------------------------------------
# Python 3.11+ ships `tomllib` in the stdlib. On 3.9/3.10 we fall back to a
# small hand-rolled parser covering the subset Codex's `config.toml` uses:
# top-level scalars (strings, bools, ints, floats), `[table]` and
# `[table.sub]` headers, arrays of scalars, inline `{ k = v, ... }` tables,
# and `# ...` comments. Anything more exotic (multi-line strings, dates,
# `[[arrays.of.tables]]`, dotted keys, hex/octal numbers) is unsupported —
# the migrator doesn't need any of it.

try:
    import tomllib  # type: ignore[import-not-found]  # Python 3.11+
except ModuleNotFoundError:
    class _MinimalToml:
        class TOMLDecodeError(ValueError):
            pass

        @classmethod
        def loads(cls, text: str) -> dict:
            return _toml_minimal_parse(text, cls.TOMLDecodeError)

    def _toml_strip_comment(line: str) -> str:
        """Strip `# ...` comments, respecting `"..."` strings."""
        in_str = False
        esc = False
        for i, c in enumerate(line):
            if esc:
                esc = False
                continue
            if c == "\\" and in_str:
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if c == "#" and not in_str:
                return line[:i].rstrip()
        return line.rstrip()

    def _toml_skip_ws(s: str, i: int) -> int:
        while i < len(s) and s[i] in " \t":
            i += 1
        return i

    def _toml_parse_string(s: str, i: int, err) -> tuple[str, int]:
        assert s[i] == '"'
        i += 1
        out: list[str] = []
        while i < len(s):
            c = s[i]
            if c == "\\":
                if i + 1 >= len(s):
                    raise err("unterminated escape in string")
                nxt = s[i + 1]
                out.append({"n": "\n", "t": "\t", "r": "\r",
                            "\\": "\\", '"': '"', "/": "/"}.get(nxt, nxt))
                i += 2
            elif c == '"':
                return "".join(out), i + 1
            else:
                out.append(c)
                i += 1
        raise err("unterminated string")

    def _toml_parse_value(s: str, i: int, err) -> tuple[object, int]:
        i = _toml_skip_ws(s, i)
        if i >= len(s):
            raise err("expected value")
        c = s[i]
        if c == '"':
            return _toml_parse_string(s, i, err)
        if c == "[":
            return _toml_parse_array(s, i, err)
        if c == "{":
            return _toml_parse_inline_table(s, i, err)
        # Bare token: bool / int / float.
        start = i
        while i < len(s) and s[i] not in " \t,]}#":
            i += 1
        token = s[start:i]
        if token == "true":
            return True, i
        if token == "false":
            return False, i
        try:
            if any(ch in token for ch in ".eE"):
                return float(token), i
            return int(token), i
        except ValueError:
            raise err(f"could not parse value: {token!r}")

    def _toml_parse_array(s: str, i: int, err) -> tuple[list, int]:
        assert s[i] == "["
        i += 1
        items: list = []
        while i < len(s):
            i = _toml_skip_ws(s, i)
            if i < len(s) and s[i] == "]":
                return items, i + 1
            val, i = _toml_parse_value(s, i, err)
            items.append(val)
            i = _toml_skip_ws(s, i)
            if i < len(s) and s[i] == ",":
                i += 1
        raise err("unterminated array")

    def _toml_parse_inline_table(s: str, i: int, err) -> tuple[dict, int]:
        assert s[i] == "{"
        i += 1
        table: dict = {}
        while i < len(s):
            i = _toml_skip_ws(s, i)
            if i < len(s) and s[i] == "}":
                return table, i + 1
            if s[i] == ",":
                i += 1
                continue
            key_start = i
            while i < len(s) and s[i] not in " \t=,}":
                i += 1
            key = s[key_start:i].strip()
            if key.startswith('"') and key.endswith('"') and len(key) >= 2:
                key = key[1:-1]
            i = _toml_skip_ws(s, i)
            if i >= len(s) or s[i] != "=":
                raise err("expected '=' in inline table")
            i += 1
            val, i = _toml_parse_value(s, i, err)
            table[key] = val
        raise err("unterminated inline table")

    def _toml_minimal_parse(text: str, err) -> dict:
        root: dict = {}
        current: dict = root
        for raw in text.splitlines():
            line = _toml_strip_comment(raw).strip()
            if not line:
                continue
            if line.startswith("["):
                end = line.find("]")
                if end < 0:
                    raise err(f"unterminated section header: {line!r}")
                path = [p.strip() for p in line[1:end].split(".")]
                current = root
                for p in path:
                    if p.startswith('"') and p.endswith('"') and len(p) >= 2:
                        p = p[1:-1]
                    current = current.setdefault(p, {})
                continue
            eq = line.find("=")
            if eq < 0:
                raise err(f"expected key = value, got: {line!r}")
            key = line[:eq].strip()
            if key.startswith('"') and key.endswith('"') and len(key) >= 2:
                key = key[1:-1]
            value, _ = _toml_parse_value(line, eq + 1, err)
            current[key] = value
        return root

    tomllib = _MinimalToml()  # type: ignore[assignment]


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
# Python's stdlib has `tomllib` for reading TOML but no writer. We only need
# to emit a small, well-defined subset (top-level scalars, nested tables for
# `[mcp_servers.NAME]` / `[sandbox_workspace_write]` / etc., arrays of
# strings, and small inline tables for env-style maps), so a hand-rolled
# writer beats taking on a dependency.

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
    """A dict with no nested dicts is rendered as an inline `{ k = v, ... }`
    table instead of as a `[parent.child]` section header. This keeps small
    env-style maps (`env = { K = "v" }`) compact and is also what Codex'
    documented schema uses for things like `shell_environment_policy.set`."""
    return all(not isinstance(v, dict) for v in d.values())


def render_toml(data: dict) -> str:
    """Render a (nested) dict to a TOML string parseable by `tomllib`."""
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
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _unique_backup_root(dst_root: Path) -> Path:
    return dst_root / "backups" / f"pre-migrate-{ts()}-{uuid.uuid4().hex[:8]}"


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
    """Inverse of frontmatter_to_meta_comment().

    Codex prompts don't have a frontmatter spec, but slash-command authors
    rely on Claude's `description` and `argument-hint` keys. We smuggle
    those across as a `<!-- migrator:meta ... -->` HTML comment so a
    Codex→Claude pass can reconstruct the frontmatter byte-for-byte.
    """
    m = MIGRATOR_META_RE.match(text)
    if not m:
        return text, None
    attrs = _parse_comment_meta(m.group("attrs"))
    return text[m.end():], (attrs or None)


def frontmatter_to_meta_comment(fm: dict) -> str:
    keep = {k: v for k, v in fm.items() if k in ("description", "argument-hint")}
    if not keep:
        return ""
    return f"<!-- migrator:meta json={json.dumps(keep, ensure_ascii=False)} -->\n"


def _parse_comment_meta(raw: str) -> dict:
    if raw.startswith("json="):
        try:
            data = json.loads(raw.removeprefix("json="))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return dict(re.findall(r'(\w[\w-]*)="((?:[^"\\]|\\.)*)"', raw))


def safe_cursor_rule_name(name: str, fallback: str = "migrated") -> str:
    """Return a safe Cursor rule basename, never a path."""
    stem = Path(str(name)).name
    if stem.endswith(".mdc"):
        stem = stem[:-4]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")
    return stem or fallback


def make_frontmatter(d: dict) -> str:
    lines = ["---"]
    for k, v in d.items():
        lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


def fenced_block(kind: str, source: str, body: str) -> str:
    """Wrap content that lives in different shapes on each side (Codex
    `instructions` as a TOML string vs. Claude `outputStyle` files) in an
    HTML comment fence so it can be embedded in the target's instruction
    document and unwrapped on the way back."""
    body = body.rstrip()
    return (f"\n\n{MIGRATOR_BEGIN.format(kind=kind, source=source)}\n"
            f"{body}\n{MIGRATOR_END}\n")


def extract_fenced(text: str, kind: str) -> tuple[str, str | None]:
    """Inverse of fenced_block(). Returns (text_with_block_removed,
    block_body_or_None) for the first matching kind."""
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
    """Shared state for a single source→destination migration pair.

    `plan_mode` is the key knob: when True, write_text/copy_file just
    record the destination path in `planned_writes` instead of touching
    disk. The migration is run twice — once in plan mode to discover every
    file we'll touch (so we can back them all up upfront), and once in
    apply mode to actually write.
    """
    src_root: Path
    dst_root: Path
    src_doc: Path | None
    dst_doc: Path | None
    dry_run: bool
    merge: bool
    backup: bool
    report: Report
    plan_mode: bool = False
    planned_writes: set[Path] = field(default_factory=set)
    backup_root: Path = field(init=False)

    def __post_init__(self) -> None:
        self.backup_root = _unique_backup_root(self.dst_root)


def write_text(ctx: Ctx, path: Path, content: str) -> None:
    """Write content to `path` — or, in plan_mode, just record that we
    would. Used by every Tier A/B function that produces output."""
    if ctx.plan_mode:
        ctx.planned_writes.add(path)
        return
    if ctx.dry_run:
        ctx.planned_writes.add(path)
        print(f"[dry-run] write {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def copy_file(ctx: Ctx, src: Path, dst: Path) -> None:
    if ctx.plan_mode:
        ctx.planned_writes.add(dst)
        return
    if ctx.dry_run:
        ctx.planned_writes.add(dst)
        print(f"[dry-run] copy {src} → {dst}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def perform_backup(ctx: Ctx) -> Path | None:
    """Back up every planned destination + write a manifest. Returns the
    backup dir (or None on dry-run / nothing to back up).

    The manifest records, for each planned destination:
      - `original`        — absolute path on disk
      - `relative`        — path inside the backup directory
      - `existed_before`  — whether the destination existed pre-migration

    `--restore` uses `existed_before` to decide whether to copy a file
    back (existing → restore) or delete the destination (newly-created by
    the migration → remove).
    """
    if not ctx.backup or not ctx.planned_writes:
        return None

    entries: list[dict] = []
    for p in sorted(ctx.planned_writes):
        try:
            rel = p.relative_to(ctx.dst_root)
        except ValueError:
            rel = Path(p.name)
        existed = p.exists()
        entries.append({
            "original": str(p),
            "relative": str(rel),
            "existed_before": existed,
        })

    if ctx.dry_run:
        for e in entries:
            if e["existed_before"]:
                ctx.report.backups.append(
                    f"would back up: {e['original']} → "
                    f"{ctx.backup_root}/{e['relative']}"
                )
        return None

    ctx.backup_root.mkdir(parents=True, exist_ok=True)
    for e in entries:
        if not e["existed_before"]:
            continue
        src = Path(e["original"])
        dest = ctx.backup_root / e["relative"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        ctx.report.backups.append(str(dest))

    manifest = {
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "direction": ctx.report.direction,
        "src_root": str(ctx.src_root),
        "dst_root": str(ctx.dst_root),
        "entries": entries,
    }
    (ctx.backup_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return ctx.backup_root


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
# Tier A translations have a near-1:1 mapping on the other side. They're
# applied unconditionally and don't trigger any user prompts. Each function
# is structured so that re-running it after an earlier write reads the
# updated destination via load_json/load_toml and layers on top — that's
# what makes plan→backup→apply correct even when multiple translations
# target the same file (e.g. settings.json).

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
    """Codex only speaks stdio MCP; Claude's SSE/HTTP servers can't be
    represented and get reported as skipped."""
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
# Cursor — I/O helpers + direction drivers
# ============================================================================
# Cursor (a VS Code fork) stores its agent config across:
#   - <cursor_root>/mcp.json                  — MCP servers (same shape as Claude)
#   - <cursor_root>/rules/*.mdc               — project rules (Markdown +
#                                                YAML frontmatter)
#   - <project_root>/.cursorrules             — legacy plain-text rules
# User scope (~/.cursor) only has global mcp.json. The Cursor IDE settings
# (themes, keybindings, extensions) live elsewhere and are deliberately out
# of scope for this migrator — those are editor config, not agent config.
#
# We translate by going through a small intermediate: a list[CursorRule]
# for instruction docs, and a dict[name, McpSpec] for MCP servers. That
# keeps cursor↔claude and cursor↔codex symmetrical.

CURSOR_RULE_BLOCK_RE = re.compile(
    r"<!--\s*migrator:begin\s+kind=cursor-rule\s+source=(?P<name>\S+)\s*-->\n"
    r"(?:<!--\s*migrator:cursor-meta\s+(?P<meta>.*?)\s*-->\n)?"
    r"(?P<body>.*?)\n?<!--\s*migrator:end\s*-->",
    re.DOTALL,
)


@dataclass
class CursorRule:
    name: str
    description: str
    globs: object  # str | list[str] | None
    always_apply: bool
    body: str


def cursor_project_root_from(cursor_root: Path) -> Path | None:
    """Cursor's `.cursorrules` (legacy) sits at the project root, alongside
    the `.cursor/` dir. For a project-scope cursor_root like `./.cursor`,
    the project root is its parent. For a user-scope root like `~/.cursor`,
    there is no legacy file location.
    """
    if cursor_root.name == ".cursor":
        return cursor_root.parent
    return None


def cursor_read_mcp(cursor_root: Path) -> dict:
    p = cursor_root / "mcp.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"warn: {p} not valid JSON ({e}); treating as empty",
              file=sys.stderr)
        return {}
    return data.get("mcpServers") or {}


def cursor_write_mcp(servers: dict, cursor_root: Path, ctx: Ctx) -> None:
    """Write MCP servers into <cursor_root>/mcp.json, merging with any
    existing `mcpServers` block."""
    p = cursor_root / "mcp.json"
    existing: dict = {}
    if p.exists() and ctx.merge:
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    existing.setdefault("mcpServers", {})
    existing["mcpServers"].update(servers)
    write_text(ctx, p, json.dumps(existing, indent=2) + "\n")


def cursor_read_rules(cursor_root: Path) -> list[CursorRule]:
    """Read both .cursor/rules/*.mdc and legacy <project>/.cursorrules."""
    rules: list[CursorRule] = []
    rules_dir = cursor_root / "rules"
    if rules_dir.is_dir():
        for f in sorted(rules_dir.rglob("*.mdc")):
            body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
            fm = fm or {}
            always = str(fm.get("alwaysApply", "false")).lower() == "true"
            globs = fm.get("globs")
            # MDC `globs` may be a comma-separated string or a YAML list;
            # we only parsed simple `k: v` lines, so commas stay literal.
            if globs and "," in globs:
                globs = [g.strip() for g in globs.split(",")]
            rules.append(CursorRule(
                name=f.stem,
                description=fm.get("description", ""),
                globs=globs,
                always_apply=always,
                body=body.strip(),
            ))
    project_root = cursor_project_root_from(cursor_root)
    if project_root:
        legacy = project_root / ".cursorrules"
        if legacy.is_file():
            rules.append(CursorRule(
                name="_cursorrules_legacy",
                description="Imported from legacy .cursorrules",
                globs=None,
                always_apply=True,
                body=legacy.read_text(encoding="utf-8").strip(),
            ))
    return rules


def cursor_rules_to_doc(rules: list[CursorRule]) -> str:
    """Concatenate Cursor rules into a single instruction doc with fenced
    metadata so a reverse migration can split them back out."""
    parts: list[str] = []
    for r in rules:
        meta_attrs: list[tuple[str, str]] = []
        if r.description:
            meta_attrs.append(("description", r.description))
        if r.globs is not None:
            g = r.globs if isinstance(r.globs, str) else ",".join(r.globs)
            meta_attrs.append(("globs", g))
        meta_attrs.append(("alwaysApply", str(r.always_apply).lower()))
        meta = {k: v for k, v in meta_attrs}
        parts.append(
            f"<!-- migrator:begin kind=cursor-rule source={safe_cursor_rule_name(r.name)} -->\n"
            f"<!-- migrator:cursor-meta json={json.dumps(meta, ensure_ascii=False)} -->\n"
            f"{r.body}\n"
            f"<!-- migrator:end -->"
        )
    return "\n\n".join(parts)


def doc_to_cursor_rules(text: str, default_name: str = "migrated") -> list[CursorRule]:
    """Inverse of cursor_rules_to_doc(). Any text outside cursor-rule
    fenced blocks becomes a single `<default_name>.mdc` with alwaysApply
    true so the content still loads in Cursor."""
    rules: list[CursorRule] = []
    spans: list[tuple[int, int]] = []
    for m in CURSOR_RULE_BLOCK_RE.finditer(text):
        attrs_str = m.group("meta") or ""
        attrs = _parse_comment_meta(attrs_str)
        globs = attrs.get("globs")
        if globs and "," in globs:
            globs = [g.strip() for g in globs.split(",")]
        rules.append(CursorRule(
            name=safe_cursor_rule_name(m.group("name")),
            description=attrs.get("description", ""),
            globs=globs,
            always_apply=attrs.get("alwaysApply", "false").lower() == "true",
            body=m.group("body").strip(),
        ))
        spans.append((m.start(), m.end()))

    # Whatever sits outside the fenced blocks is loose content; package it
    # as one alwaysApply rule so nothing is silently dropped.
    leftover_parts: list[str] = []
    last = 0
    for s, e in spans:
        chunk = text[last:s].strip()
        if chunk:
            leftover_parts.append(chunk)
        last = e
    tail = text[last:].strip()
    if tail:
        leftover_parts.append(tail)
    leftover = "\n\n".join(leftover_parts).strip()
    if leftover:
        rules.append(CursorRule(
            name=safe_cursor_rule_name(default_name), description="Migrated content",
            globs=None, always_apply=True, body=leftover,
        ))
    return rules


def cursor_write_rules(rules: list[CursorRule], cursor_root: Path,
                       ctx: Ctx) -> None:
    """Write Cursor rules as .mdc files under <cursor_root>/rules/."""
    rules_dir = cursor_root / "rules"
    for r in rules:
        fm: dict = {}
        if r.description:
            fm["description"] = r.description
        if r.globs is not None:
            fm["globs"] = (r.globs if isinstance(r.globs, str)
                           else ",".join(r.globs))
        fm["alwaysApply"] = str(r.always_apply).lower()
        body = make_frontmatter(fm) + r.body.rstrip() + "\n"
        write_text(ctx, rules_dir / f"{safe_cursor_rule_name(r.name)}.mdc", body)


def _cursor_label_mcp(direction: str, mcp: dict) -> str:
    return (f"mcpServers ({len(mcp)} entr{'y' if len(mcp) == 1 else 'ies'}) "
            f"{direction}")


def _normalize_mcp_for_cursor(spec: dict) -> dict:
    """Cursor accepts the Claude shape verbatim (stdio + SSE/HTTP). Keep
    documented keys only — drop anything migrator-internal."""
    out: dict = {}
    for k in ("command", "args", "env", "type", "url"):
        if k in spec and spec[k] is not None:
            out[k] = spec[k]
    return out


def _native_mcp_from_codex(name: str, spec: dict) -> dict:
    """Codex stores stdio MCP under a TOML table; produce a Claude/Cursor
    JSON-shaped dict."""
    out: dict = {"type": "stdio"}
    for k in ("command", "args", "env"):
        if spec.get(k):
            out[k] = list(spec[k]) if k == "args" else (
                dict(spec[k]) if k == "env" else spec[k])
    return out


# ---- claude → cursor -------------------------------------------------------

def run_claude_to_cursor(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    # Tier A: instruction doc.
    src_doc = (ctx.src_doc if ctx.src_doc and ctx.src_doc.exists()
               else ctx.src_root / "CLAUDE.md")
    if src_doc.exists():
        text = src_doc.read_text(encoding="utf-8")
        rules = doc_to_cursor_rules(text)
        if rules:
            cursor_write_rules(rules, ctx.dst_root, ctx)
            ctx.report.migrated_clean.append(
                f"{src_doc.name} → {len(rules)} cursor rule file(s)")

    # Tier A: MCP servers (Claude's mcpServers → Cursor's mcp.json).
    settings = load_claude_settings(ctx.src_root)
    mcp = settings.get("mcpServers") or {}
    if mcp:
        out = {n: _normalize_mcp_for_cursor(s) for n, s in mcp.items()}
        cursor_write_mcp(out, ctx.dst_root, ctx)
        ctx.report.migrated_clean.append(_cursor_label_mcp("→ cursor mcp.json", out))

    # Settings keys with no Cursor equivalent (truly Tier C).
    for key in ("hooks", "permissions", "statusLine", "outputStyle"):
        if settings.get(key):
            ctx.report.skipped_unmappable.append(
                f"settings.json:{key} (Cursor has no equivalent)")
    # `plugins/` is the only source dir with no Tier B option — agents,
    # skills, and commands are handled by their cursor-bound Tier B
    # options below.
    plugins_dir = ctx.src_root / "plugins"
    if plugins_dir.is_dir() and any(plugins_dir.iterdir()):
        ctx.report.skipped_unmappable.append(
            "plugins/ (Cursor has no equivalent)")

    # Tier B options applicable in this direction.
    _run_lossy(ctx, lossy_decisions, "claude->cursor")


# ---- cursor → claude -------------------------------------------------------

def run_cursor_to_claude(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    # Tier A: rules → CLAUDE.md (fenced so the round-trip survives).
    rules = cursor_read_rules(ctx.src_root)
    if rules:
        doc = cursor_rules_to_doc(rules)
        dst_doc = ctx.dst_doc or (ctx.dst_root / "CLAUDE.md")
        if dst_doc.exists() and ctx.merge:
            doc = dst_doc.read_text(encoding="utf-8").rstrip() + "\n\n" + doc
        write_text(ctx, dst_doc, doc + "\n")
        ctx.report.migrated_clean.append(
            f"{len(rules)} cursor rule(s) → {dst_doc.name}")

    # Tier A: MCP.
    mcp = cursor_read_mcp(ctx.src_root)
    if mcp:
        dst = ctx.dst_root / "settings.json"
        existing = load_json(dst) if (ctx.merge and dst.exists()) else {}
        existing.setdefault("mcpServers", {}).update(
            {n: _normalize_mcp_for_cursor(s) for n, s in mcp.items()})
        # Claude expects a `type` key; default to stdio if missing.
        for n, s in existing["mcpServers"].items():
            s.setdefault("type", "stdio")
        write_text(ctx, dst, json.dumps(existing, indent=2) + "\n")
        ctx.report.migrated_clean.append(
            _cursor_label_mcp("→ settings.json:mcpServers", mcp))

    _run_lossy(ctx, lossy_decisions, "cursor->claude")


# ---- codex → cursor --------------------------------------------------------

def run_codex_to_cursor(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    # Tier A: AGENTS.md → cursor rules.
    src_doc = (ctx.src_doc if ctx.src_doc and ctx.src_doc.exists()
               else ctx.src_root / "AGENTS.md")
    if src_doc.exists():
        text = src_doc.read_text(encoding="utf-8")
        rules = doc_to_cursor_rules(text)
        if rules:
            cursor_write_rules(rules, ctx.dst_root, ctx)
            ctx.report.migrated_clean.append(
                f"{src_doc.name} → {len(rules)} cursor rule file(s)")

    # Tier A: MCP (TOML → Cursor JSON).
    cfg = load_toml(ctx.src_root / "config.toml")
    mcp = cfg.get("mcp_servers") or {}
    if mcp:
        out = {n: _native_mcp_from_codex(n, s) for n, s in mcp.items()}
        cursor_write_mcp(out, ctx.dst_root, ctx)
        ctx.report.migrated_clean.append(_cursor_label_mcp("→ cursor mcp.json", out))

    # Codex-only items with no Cursor equivalent.
    for k in ("approval_policy", "sandbox_mode", "sandbox_workspace_write",
              "shell_environment_policy", "profiles",
              "model_reasoning_effort", "notify"):
        if k in cfg:
            ctx.report.skipped_unmappable.append(
                f"config.toml:{k} (Cursor has no equivalent)")

    _run_lossy(ctx, lossy_decisions, "codex->cursor")


# ---- cursor → codex --------------------------------------------------------

def run_cursor_to_codex(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    rules = cursor_read_rules(ctx.src_root)
    if rules:
        doc = cursor_rules_to_doc(rules)
        dst_doc = ctx.dst_doc or (ctx.dst_root / "AGENTS.md")
        if dst_doc.exists() and ctx.merge:
            doc = dst_doc.read_text(encoding="utf-8").rstrip() + "\n\n" + doc
        write_text(ctx, dst_doc, doc + "\n")
        ctx.report.migrated_clean.append(
            f"{len(rules)} cursor rule(s) → {dst_doc.name}")

    mcp = cursor_read_mcp(ctx.src_root)
    if mcp:
        dst = ctx.dst_root / "config.toml"
        existing = load_toml(dst) if (ctx.merge and dst.exists()) else {}
        existing.setdefault("mcp_servers", {})
        for name, spec in mcp.items():
            t = (spec.get("type") or "stdio").lower()
            if t != "stdio":
                ctx.report.skipped_unmappable.append(
                    f"MCP server '{name}' uses type='{t}' "
                    "(Codex only supports stdio)")
                continue
            out: dict = {}
            if "command" in spec:
                out["command"] = spec["command"]
            if spec.get("args"):
                out["args"] = list(spec["args"])
            if spec.get("env"):
                out["env"] = dict(spec["env"])
            if out.get("command"):
                existing["mcp_servers"][name] = out
        write_text(ctx, dst, render_toml(existing))
        ctx.report.migrated_clean.append(
            _cursor_label_mcp("→ config.toml:[mcp_servers.*]", mcp))

    _run_lossy(ctx, lossy_decisions, "cursor->codex")


def _run_lossy(ctx: Ctx, decisions: dict[str, bool], direction_key: str) -> None:
    """Shared Tier B runner: iterate the catalog, applying detected items
    the user accepted and recording the rest as declined."""
    for opt in TIER_B:
        if opt.direction != direction_key or not opt.detect(ctx):
            continue
        if decisions.get(opt.id):
            opt.apply(ctx)
        else:
            ctx.report.skipped_by_user.append(f"{opt.label} (declined)")


# ============================================================================
# Tier B — lossy translations (user-confirmed)
# ============================================================================
# Tier B items don't have an exact equivalent on the other side, so the
# migration is heuristic. Each option's `detect` says whether the relevant
# source state exists, `preview` produces a one-line "here's what will
# happen" string for the preflight UI, and `apply` performs the
# translation. The user accepts or skips each one (interactively, or via
# --apply-lossy / --skip-lossy in non-interactive mode).

@dataclass
class LossyOption:
    id: str
    direction: str  # e.g. 'claude->codex', 'cursor->claude', 'codex->cursor'
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
    """Heuristic: Claude's per-tool allow/deny patterns are richer than
    Codex's coarse sandbox modes. We classify the rules into a closest-fit
    sandbox mode + extract Write() patterns into writable_roots + deny of
    WebFetch/WebSearch into network_access=false. Round-trip is *not*
    byte-identical — that's the lossy part."""
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
    """Extract the first runnable shell command from a Claude hook config.

    Claude hooks are: [{matcher, hooks: [{type:'command', command:'...'}]}].
    Codex `notify` takes an argv-style list. We wrap the user's shell
    command in `/bin/sh -c` rather than trying to tokenize it, since
    Claude commands are meant to be shell-parsed (with redirects, pipes,
    `$VAR` expansion, etc.).
    """
    for grp in entries or []:
        for h in (grp.get("hooks") or []) if isinstance(grp, dict) else []:
            if h.get("type") == "command" and h.get("command"):
                return ["/bin/sh", "-c", h["command"]]
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


# ---- B6/7/8/9: claude/codex subagents, skills, commands, prompts → cursor rules ----
# Cursor has no subagent runtime, no skills system, and no slash-command
# concept. The next best thing is a Cursor rule (`.cursor/rules/*.mdc`)
# with `alwaysApply: false` so the content is loadable when relevant but
# not auto-prepended. The result is lossy in the same way as
# agents→codex prompts: the invocation/runtime semantics don't survive,
# only the text content does.

def _flatten_dir_to_cursor_rules(ctx: Ctx, src_subdir: str, name_prefix: str,
                                  fallback_desc_tpl: str, lossy_note: str,
                                  always_apply: bool = False) -> None:
    """Shared body for the four 'flatten claude/codex source dir into Cursor
    rules' lossy options. Writes one `<prefix>-<name>.mdc` per source file
    under `<dst_root>/rules/`."""
    src_dir = ctx.src_root / src_subdir
    rules_dir = ctx.dst_root / "rules"
    for f in sorted(src_dir.rglob("*.md")):
        rel = f.relative_to(src_dir)
        stem = rel.as_posix().replace("/", "-").rsplit(".", 1)[0]
        out_name = f"{name_prefix}-{stem}.mdc"
        body, fm = strip_frontmatter(f.read_text(encoding="utf-8"))
        desc = (fm or {}).get("description") or fallback_desc_tpl.format(name=stem)
        front_fm = {"description": desc, "alwaysApply": str(always_apply).lower()}
        write_text(ctx, rules_dir / out_name,
                   make_frontmatter(front_fm) + body.lstrip("\n"))
        ctx.report.migrated_lossy.append(
            f"{src_subdir}/{rel} → rules/{out_name} ({lossy_note})")


# ---- agents → cursor rules ----
def _detect_claude_agents_cursor(ctx: Ctx) -> bool:
    p = ctx.src_root / "agents"
    return p.is_dir() and any(p.rglob("*.md"))


def _preview_claude_agents_cursor(ctx: Ctx) -> str:
    n = sum(1 for _ in (ctx.src_root / "agents").rglob("*.md"))
    return (f"{n} subagent file(s) → .cursor/rules/agent-*.mdc "
            "(alwaysApply:false; loses subagent runtime)")


def _apply_claude_agents_cursor(ctx: Ctx) -> None:
    _flatten_dir_to_cursor_rules(
        ctx, src_subdir="agents", name_prefix="agent",
        fallback_desc_tpl="Migrated from Claude subagent {name}",
        lossy_note="lossy: no subagent runtime in Cursor",
    )


# ---- skills → cursor rules ----
def _detect_claude_skills_cursor(ctx: Ctx) -> bool:
    p = ctx.src_root / "skills"
    return p.is_dir() and any(p.glob("*/SKILL.md"))


def _preview_claude_skills_cursor(ctx: Ctx) -> str:
    n = sum(1 for _ in (ctx.src_root / "skills").glob("*/SKILL.md"))
    return (f"{n} skill(s) → .cursor/rules/skill-*.mdc "
            "(alwaysApply:false; bundled assets are not migrated)")


def _apply_claude_skills_cursor(ctx: Ctx) -> None:
    src_dir = ctx.src_root / "skills"
    rules_dir = ctx.dst_root / "rules"
    for skill_md in sorted(src_dir.glob("*/SKILL.md")):
        name = skill_md.parent.name
        body, fm = strip_frontmatter(skill_md.read_text(encoding="utf-8"))
        desc = (fm or {}).get("description") or f"Migrated from Claude skill {name}"
        front = make_frontmatter({"description": desc, "alwaysApply": "false"})
        # Note bundled assets so the user knows what isn't migrated.
        assets = [p for p in skill_md.parent.rglob("*")
                  if p.is_file() and p.name != "SKILL.md"]
        prelude = ""
        if assets:
            prelude = (f"<!-- Original skill bundled {len(assets)} asset "
                       f"file(s); not migrated. -->\n\n")
        write_text(ctx, rules_dir / f"skill-{name}.mdc",
                   front + prelude + body.lstrip("\n"))
        ctx.report.migrated_lossy.append(
            f"skills/{name}/ → rules/skill-{name}.mdc "
            f"(lossy: no skills runtime in Cursor; "
            f"{len(assets)} asset file(s) not migrated)")


# ---- claude commands → cursor rules ----
def _detect_claude_commands_cursor(ctx: Ctx) -> bool:
    p = ctx.src_root / "commands"
    return p.is_dir() and any(p.rglob("*.md"))


def _preview_claude_commands_cursor(ctx: Ctx) -> str:
    n = sum(1 for _ in (ctx.src_root / "commands").rglob("*.md"))
    return (f"{n} slash command(s) → .cursor/rules/command-*.mdc "
            "(alwaysApply:false; not invocable like Claude slash commands)")


def _apply_claude_commands_cursor(ctx: Ctx) -> None:
    _flatten_dir_to_cursor_rules(
        ctx, src_subdir="commands", name_prefix="command",
        fallback_desc_tpl="Slash command: /{name}",
        lossy_note="lossy: Cursor rules aren't invocable like /commands",
    )


# ---- codex prompts → cursor rules ----
def _detect_codex_prompts_cursor(ctx: Ctx) -> bool:
    p = ctx.src_root / "prompts"
    return p.is_dir() and any(p.rglob("*.md"))


def _preview_codex_prompts_cursor(ctx: Ctx) -> str:
    n = sum(1 for _ in (ctx.src_root / "prompts").rglob("*.md"))
    return (f"{n} prompt(s) → .cursor/rules/prompt-*.mdc "
            "(alwaysApply:false; not on-demand invocable like Codex prompts)")


def _apply_codex_prompts_cursor(ctx: Ctx) -> None:
    _flatten_dir_to_cursor_rules(
        ctx, src_subdir="prompts", name_prefix="prompt",
        fallback_desc_tpl="Codex prompt: {name}",
        lossy_note="lossy: Cursor rules aren't on-demand invocable",
    )


# ---- Catalog ---------------------------------------------------------------

TIER_B: list[LossyOption] = [
    LossyOption(
        id="permissions",
        direction="claude->codex",
        label="permissions → sandbox_mode + approval_policy",
        rationale=("Claude's per-tool regex permissions don't map exactly to "
                   "Codex's coarse sandbox modes. We infer the closest match."),
        detect=_detect_claude_permissions,
        preview=_preview_claude_permissions,
        apply=_apply_claude_permissions,
    ),
    LossyOption(
        id="sandbox",
        direction="codex->claude",
        label="sandbox_mode/approval_policy → permissions",
        rationale=("Codex's coarse sandbox mode is expanded into a set of "
                   "Claude allow/deny patterns. Round-trip is not exact."),
        detect=_detect_codex_sandbox,
        preview=_preview_codex_sandbox,
        apply=_apply_codex_sandbox,
    ),
    LossyOption(
        id="hooks",
        direction="claude->codex",
        label="hooks.Notification/Stop → notify",
        rationale=("Codex `notify` covers a subset of Claude's hook events. "
                   "Other hook types (PreToolUse, etc.) are dropped."),
        detect=_detect_claude_notify_hook,
        preview=_preview_claude_notify_hook,
        apply=_apply_claude_notify_hook,
    ),
    LossyOption(
        id="notify",
        direction="codex->claude",
        label="notify → hooks.Notification",
        rationale=("Codex's single notify program is registered as a Claude "
                   "Notification hook without a matcher."),
        detect=_detect_codex_notify,
        preview=_preview_codex_notify,
        apply=_apply_codex_notify,
    ),
    LossyOption(
        id="agents",
        direction="claude->codex",
        label="agents/ → prompts/agent-*.md",
        rationale=("Codex has no subagent system. Each agent.md is flattened "
                   "into a plain prompt; the subagent runtime is lost."),
        detect=_detect_claude_agents,
        preview=_preview_claude_agents,
        apply=_apply_claude_agents,
    ),
    LossyOption(
        id="skills",
        direction="claude->codex",
        label="skills/ → prompts/skill-*.md",
        rationale=("Codex has no skills system. SKILL.md becomes a flat prompt; "
                   "bundled assets are not migrated and skill auto-discovery is lost."),
        detect=_detect_claude_skills,
        preview=_preview_claude_skills,
        apply=_apply_claude_skills,
    ),
    LossyOption(
        id="profiles",
        direction="codex->claude",
        label="[profiles.*] → ~/.claude/profiles/*.settings.json",
        rationale=("Claude has no profile runtime. Each Codex profile is "
                   "materialized as a standalone settings file you can copy "
                   "over settings.json to activate."),
        detect=_detect_codex_profiles,
        preview=_preview_codex_profiles,
        apply=_apply_codex_profiles,
    ),
    LossyOption(
        id="agents_cursor",
        direction="claude->cursor",
        label="agents/ → .cursor/rules/agent-*.mdc",
        rationale=("Cursor has no subagent runtime. Each agent.md becomes a "
                   "Cursor rule with alwaysApply:false — content survives, "
                   "but subagent invocation semantics don't."),
        detect=_detect_claude_agents_cursor,
        preview=_preview_claude_agents_cursor,
        apply=_apply_claude_agents_cursor,
    ),
    LossyOption(
        id="skills_cursor",
        direction="claude->cursor",
        label="skills/ → .cursor/rules/skill-*.mdc",
        rationale=("Cursor has no skills runtime. SKILL.md becomes a Cursor "
                   "rule with alwaysApply:false; bundled assets are not "
                   "migrated and auto-discovery is lost."),
        detect=_detect_claude_skills_cursor,
        preview=_preview_claude_skills_cursor,
        apply=_apply_claude_skills_cursor,
    ),
    LossyOption(
        id="commands_cursor",
        direction="claude->cursor",
        label="commands/ → .cursor/rules/command-*.mdc",
        rationale=("Cursor has no slash-command equivalent. Commands become "
                   "alwaysApply:false rules — content is loadable but won't "
                   "be invokable as /commandname."),
        detect=_detect_claude_commands_cursor,
        preview=_preview_claude_commands_cursor,
        apply=_apply_claude_commands_cursor,
    ),
    LossyOption(
        id="prompts_cursor",
        direction="codex->cursor",
        label="prompts/ → .cursor/rules/prompt-*.mdc",
        rationale=("Cursor has no on-demand prompt invocation. Codex prompts "
                   "become alwaysApply:false rules — content is loadable but "
                   "loses its on-demand semantics."),
        detect=_detect_codex_prompts_cursor,
        preview=_preview_codex_prompts_cursor,
        apply=_apply_codex_prompts_cursor,
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
    """Phase 1 of migration: scan source, preview Tier A, and decide each
    Tier B item. Returns {option_id: should_apply} for the apply pass.

    Decision precedence: --apply-lossy/--skip-lossy override all; else if
    running interactively, ask per item; else accept everything (the
    non-interactive default is to migrate as much as we can).
    """
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
    _run_lossy(ctx, lossy_decisions, "claude->codex")

    for sub in ("plugins",):
        p = ctx.src_root / sub
        if p.is_dir() and any(p.iterdir()):
            ctx.report.skipped_unmappable.append(f"{sub}/ (Claude-only runtime)")


def run_codex_to_claude(ctx: Ctx, lossy_decisions: dict[str, bool]) -> None:
    tier_a_docs_codex_to_claude(ctx)
    tier_a_prompts_to_commands(ctx)
    tier_a_settings_codex_to_claude(ctx)
    _run_lossy(ctx, lossy_decisions, "codex->claude")


# Dispatch table for (from_tool, to_tool) pairs. All six directed pairs of
# {claude, codex, cursor} are populated; same-tool pairs are rejected at the
# CLI before reaching this dict.
RUNNERS: dict[tuple[str, str], Callable[[Ctx, dict[str, bool]], None]] = {
    ("claude", "codex"):  run_claude_to_codex,
    ("codex",  "claude"): run_codex_to_claude,
    ("claude", "cursor"): run_claude_to_cursor,
    ("cursor", "claude"): run_cursor_to_claude,
    ("codex",  "cursor"): run_codex_to_cursor,
    ("cursor", "codex"):  run_cursor_to_codex,
}


# ============================================================================
# Restore
# ============================================================================

def find_latest_backup() -> Path | None:
    """Return the most recently created migration backup across every
    known tool's backups dir (user + project scope). Ranks by the
    `created_at` field in the manifest, with mtime as a tiebreak, so the
    correct "last migration" wins even when filesystems have coarse mtime
    resolution or when backups have been copied around.
    """
    bases: list[Path] = []
    for tool in TOOLS:
        bases.append(Path.home() / f".{tool}" / "backups")
        bases.append(Path.cwd() / f".{tool}" / "backups")

    candidates: list[tuple[str, float, Path]] = []
    for b in bases:
        if not b.is_dir():
            continue
        for p in b.glob("pre-migrate-*"):
            manifest = p / "manifest.json"
            if not manifest.exists():
                continue
            created_at = ""
            try:
                created_at = json.loads(
                    manifest.read_text(encoding="utf-8")
                ).get("created_at", "")
            except json.JSONDecodeError:
                pass
            candidates.append((created_at, p.stat().st_mtime, p))
    if not candidates:
        return None
    return max(candidates)[2]


def restore_from_backup(backup_path: Path, interactive: bool,
                        dry_run: bool) -> int:
    manifest_path = backup_path / "manifest.json"
    if not manifest_path.exists():
        print(f"No manifest.json at {backup_path}", file=sys.stderr)
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"Could not parse manifest: {e}", file=sys.stderr)
        return 1

    entries = manifest.get("entries", [])
    to_restore = [e for e in entries if e.get("existed_before")]
    to_delete = [e for e in entries if not e.get("existed_before")]

    print("=" * 72)
    print(f"Restore plan — backup at {backup_path}")
    print("=" * 72)
    print(f"Original migration: {manifest.get('direction', '?')}")
    print(f"Backup created:     {manifest.get('created_at', '?')}")
    print()
    print(f"Will restore {len(to_restore)} file(s) (overwriting current state):")
    for e in to_restore:
        print(f"  ← {e['original']}")
    print()
    print(f"Will delete {len(to_delete)} file(s) (created by the migration):")
    for e in to_delete:
        print(f"  ✗ {e['original']}")
    print()

    if interactive and not ask_yn("Proceed with restore?", True):
        print("Aborted.")
        return 0

    restored, deleted, missing = [], [], []
    for e in to_restore:
        backup_file = backup_path / e["relative"]
        original = Path(e["original"])
        if not backup_file.exists():
            missing.append(e["original"])
            continue
        if dry_run:
            print(f"[dry-run] restore {backup_file} → {original}")
        else:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_file, original)
        restored.append(e["original"])

    for e in to_delete:
        original = Path(e["original"])
        if not original.exists():
            continue
        if dry_run:
            print(f"[dry-run] delete {original}")
        else:
            original.unlink()
        deleted.append(e["original"])

    print()
    print(f"Restored: {len(restored)}")
    print(f"Deleted:  {len(deleted)}")
    if missing:
        print(f"WARNING — backup files missing for: {missing}", file=sys.stderr)
        return 2
    return 0


TOOLS = ("claude", "codex", "cursor")


def _tool_paths(tool: str, scope: str, override_dir: str | None) -> dict:
    """Return {'root': Path, 'doc': Path | None} for a tool at a given scope.

    `root`  — the tool's configuration dir for this scope.
    `doc`   — the conventional project instruction file alongside (e.g.
              ./CLAUDE.md or ./AGENTS.md). None when not applicable
              (user scope, or tools without a sibling doc).
    """
    if override_dir:
        return {"root": Path(override_dir).expanduser(), "doc": None}
    home = Path.home()
    cwd = Path.cwd()
    if tool == "claude":
        return ({"root": home / ".claude", "doc": None} if scope == "user"
                else {"root": cwd / ".claude", "doc": cwd / "CLAUDE.md"})
    if tool == "codex":
        return ({"root": home / ".codex", "doc": None} if scope == "user"
                else {"root": cwd / ".codex", "doc": cwd / "AGENTS.md"})
    if tool == "cursor":
        # Cursor has no user-level rules — only global MCP at ~/.cursor/mcp.json.
        # Project rules live in <repo>/.cursor/rules/*.mdc and the legacy
        # <repo>/.cursorrules sits alongside.
        return ({"root": home / ".cursor", "doc": None} if scope == "user"
                else {"root": cwd / ".cursor", "doc": cwd / ".cursorrules"})
    raise ValueError(f"unknown tool: {tool}")


def _csv_set(s: str | None) -> set[str] | None:
    if not s:
        return None
    return {x.strip() for x in s.split(",") if x.strip()}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--from", dest="from_tool", choices=TOOLS,
                    help="Source tool to migrate from.")
    ap.add_argument("--to", dest="to_tool", choices=TOOLS,
                    help="Destination tool to migrate to.")
    ap.add_argument("--direction",
                    help="Backward-compat shorthand for --from/--to, e.g. "
                    "'claude-to-codex' or 'cursor-to-claude'.")
    ap.add_argument("--restore", nargs="?", const="__latest__",
                    metavar="BACKUP_DIR",
                    help="Restore from a previous migration backup. Pass a "
                    "specific backup directory, or omit to use the latest "
                    "one under any tool's backups dir.")
    ap.add_argument("--scope", choices=["user", "project", "both"],
                    default="user")
    ap.add_argument("--claude-dir")
    ap.add_argument("--codex-dir")
    ap.add_argument("--cursor-dir")
    ap.add_argument("--dry-run", action="store_true")
    mg = ap.add_mutually_exclusive_group()
    mg.add_argument("--merge", dest="merge", action="store_true", default=True)
    mg.add_argument("--overwrite", dest="merge", action="store_false")
    ap.add_argument("--no-backup", dest="backup", action="store_false", default=True)
    ap.add_argument("--no-interactive", action="store_true")
    ap.add_argument("--apply-lossy", metavar="IDS",
                    help="Comma-separated Tier B IDs to apply (or 'all'). "
                    "IDs: " + ", ".join(o.id for o in TIER_B))
    ap.add_argument("--skip-lossy", metavar="IDS",
                    help="Comma-separated Tier B IDs to skip (or 'all').")
    args = ap.parse_args()

    # Resolve from/to from either --from/--to or legacy --direction.
    if args.direction and (args.from_tool or args.to_tool):
        ap.error("use --from/--to or --direction, not both")
    if args.direction:
        if "-to-" not in args.direction:
            ap.error(f"invalid --direction: {args.direction!r}; "
                     "expected e.g. 'claude-to-codex'")
        f, _, t = args.direction.partition("-to-")
        if f not in TOOLS or t not in TOOLS:
            ap.error(f"unknown tool(s) in --direction: {args.direction!r}")
        args.from_tool, args.to_tool = f, t

    have_migrate = args.from_tool and args.to_tool
    have_restore = args.restore is not None
    if not have_migrate and not have_restore:
        ap.error("either --from + --to, --direction, or --restore is required")
    if have_migrate and have_restore:
        ap.error("--from/--to and --restore are mutually exclusive")
    if have_migrate and args.from_tool == args.to_tool:
        ap.error("--from and --to must differ")

    interactive_default = not args.no_interactive and is_interactive()

    # ---- Restore mode ------------------------------------------------------
    if args.restore is not None:
        if args.restore == "__latest__":
            backup_path = find_latest_backup()
            if not backup_path:
                print("No backups found under ~/.claude/backups or "
                      "~/.codex/backups.", file=sys.stderr)
                return 1
            print(f"Using latest backup: {backup_path}")
        else:
            backup_path = Path(args.restore).expanduser()
            if not backup_path.is_dir():
                print(f"Not a directory: {backup_path}", file=sys.stderr)
                return 1
        return restore_from_backup(backup_path, interactive_default, args.dry_run)

    # ---- Migrate mode ------------------------------------------------------
    apply_set = _csv_set(args.apply_lossy)
    skip_set = _csv_set(args.skip_lossy)
    interactive = interactive_default and not (apply_set or skip_set)

    from_tool, to_tool = args.from_tool, args.to_tool
    direction_key = f"{from_tool}->{to_tool}"

    runner = RUNNERS.get((from_tool, to_tool))
    if runner is None:
        ap.error(f"unsupported direction: {direction_key}")

    overrides = {
        "claude": args.claude_dir,
        "codex": args.codex_dir,
        "cursor": args.cursor_dir,
    }
    scopes = ["user", "project"] if args.scope == "both" else [args.scope]

    for scope in scopes:
        src = _tool_paths(from_tool, scope, overrides[from_tool])
        dst = _tool_paths(to_tool, scope, overrides[to_tool])
        src_root, dst_root = src["root"], dst["root"]
        src_doc, dst_doc = src["doc"], dst["doc"]

        pretty = {"claude": "Claude Code", "codex": "Codex CLI", "cursor": "Cursor"}
        label = (f"{pretty[from_tool]} → {pretty[to_tool]} "
                 f"({src_root} → {dst_root})")

        if not src_root.exists() and not (src_doc and src_doc.exists()):
            print(f"\n--- {label}\n(no source files — skipped)")
            continue

        report = Report(direction=label)
        ctx = Ctx(src_root=src_root, dst_root=dst_root,
                  src_doc=src_doc, dst_doc=dst_doc,
                  dry_run=args.dry_run, merge=args.merge,
                  backup=args.backup, report=report)

        # Step 1: interactive preflight (decides which Tier B items run).
        decisions = preflight(ctx, direction_key, apply_set, skip_set, interactive)

        # Step 2: PLAN pass — re-run the migration with plan_mode=True so
        # write_text/copy_file just record destination paths. This lets us
        # back up the full set of files atomically (and show the user the
        # complete list of changes) before any writes happen.
        ctx.plan_mode = True
        runner(ctx, decisions)
        ctx.planned_writes.add(dst_root / "MIGRATION_REPORT.md")
        planned = sorted(ctx.planned_writes)

        print()
        print("Planned destination files ({}):".format(len(planned)))
        for p in planned:
            tag = "modify" if p.exists() else "create"
            print(f"  [{tag}] {p}")
        print()

        # Step 3: BACKUP everything that already exists + write manifest.
        if args.backup and planned and not args.dry_run:
            backup_root = perform_backup(ctx)
            if backup_root:
                print(f"Backup written to: {backup_root}")
                print(f"  manifest: {backup_root / 'manifest.json'}")
                print(f"  to restore later: "
                      f"python3 migrate.py --restore {backup_root}")
                print()
        elif args.backup and args.dry_run:
            perform_backup(ctx)  # populates report.backups with "would back up" notes

        if interactive and not ask_yn("Apply migration?", True):
            print("Aborted by user (no changes written; backup retained).")
            continue

        # The plan pass populated the report (Tier A/B functions don't
        # know they're being dry-run). Clear those entries so the apply
        # pass produces a clean report. `backups` is preserved.
        ctx.report.migrated_clean.clear()
        ctx.report.migrated_lossy.clear()
        ctx.report.skipped_by_user.clear()
        ctx.report.skipped_unmappable.clear()
        ctx.report.notes.clear()

        # Step 4: APPLY pass — really write.
        ctx.plan_mode = False
        ctx.planned_writes.clear()
        print(f"--- Running: {label}")
        runner(ctx, decisions)

        # Step 5: write report.
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
