"""HookBus-AgentSpend, free HookBus subscriber that tracks AI agent token usage and cost.

Built for the OpenClaw cost-refugee crisis (April 2026). Works with any HookBus
publisher (OpenClaw, Hermes, Claude Code, Cursor, Amp, OpenAI Agents SDK).

Accepts HookBus events at POST /event. If publisher metadata reports
tokens_input/tokens_output/model, uses exact figures. Otherwise falls back to
a character-based estimate so the dashboard is useful even when publishers do
not report usage.

Dashboard at GET / shows today's spend, monthly total, per-model breakdown,
per-session breakdown, and a live event stream. In-memory + SQLite persisted.

Licence: MIT. Copyright 2026 Agentic Thinking Ltd.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

__version__ = "0.2.1"

DB_PATH = Path(os.environ.get("AGENTSPEND_DB", str(Path.home() / ".agentspend" / "events.db")))
RETENTION_DAYS = int(os.environ.get("AGENTSPEND_RETENTION_DAYS", "90"))
VACUUM_THRESHOLD_MB = int(os.environ.get("AGENTSPEND_VACUUM_MB", "128"))


def _load_token() -> str:
    """Load auth token. HOOKBUS_TOKEN env var takes priority, falls back to shared
    /root/.hookbus/.token file if mounted (same token as the bus).

    On cold start (docker compose up), the bus may not have written the token
    file yet. Wait up to AGENTSPEND_TOKEN_WAIT seconds (default 30) for it to
    appear before giving up. This avoids a FATAL crash-loop on first boot."""
    env = os.environ.get("HOOKBUS_TOKEN", "").strip()
    if env:
        return env
    shared = Path(os.environ.get("HOOKBUS_TOKEN_PATH", "/root/.hookbus/.token"))
    wait_s = int(os.environ.get("AGENTSPEND_TOKEN_WAIT", "30"))
    deadline = time.time() + wait_s
    logged = False
    while time.time() < deadline:
        try:
            if shared.exists():
                content = shared.read_text().strip()
                if content:
                    return content
        except Exception:
            pass
        if not logged:
            print(f"[agentspend] waiting up to {wait_s}s for token file at {shared} "
                  f"(bus may still be booting)...", flush=True)
            logged = True
        time.sleep(1)
    return ""


_AUTH_TOKEN = _load_token()
if not _AUTH_TOKEN:
    import sys as _sys
    _sys.stderr.write(
        "FATAL: hookbus-agentspend could not find an auth token. "
        "Set HOOKBUS_TOKEN env or mount /root/.hookbus shared with the bus.\n"
    )
    raise SystemExit(1)
PORT = int(os.environ.get("AGENTSPEND_PORT", "8879"))
logger = logging.getLogger(__name__)
HOST = os.environ.get("AGENTSPEND_HOST", "0.0.0.0")

# Model prices in USD per 1M tokens (input, output) as of April 2026.
# Override via AGENTSPEND_PRICING env var pointing to a JSON file.
DEFAULT_PRICES: Dict[str, Tuple[float, float]] = {
    "claude-opus-4-7": (15.00, 75.00),
    "claude-opus-4-6": (15.00, 75.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (0.80, 4.00),
    "gpt-5-4": (5.00, 15.00),
    "gpt-5-3": (2.50, 10.00),
    "gpt-4o": (2.50, 10.00),
    "gemini-3-pro": (1.25, 5.00),
    "gemini-3-flash": (0.15, 0.60),
    "deepseek-v3": (0.27, 1.10),
    "glm-5": (0.50, 1.50),
    "glm-4-7": (0.30, 1.00),
    "kimi-k2-5": (0.40, 1.80),
    "minimax-m2-7": (0.30, 1.20),
    "minimax-m2-5": (0.30, 1.20),
    "mimo-v2-pro": (0.00, 0.00),  # free tier promo April 2026
}

# Rough estimate: 1 token = 4 chars of English. Used when publisher does not report tokens.
CHARS_PER_TOKEN = 4.0
FALLBACK_MODEL = "unknown"


PRICES_BUNDLE_DATE = "2026-04-18"  # when DEFAULT_PRICES was last reviewed
PRICES_SOURCE = "defaults"  # "defaults" or absolute path to override JSON
PRICES_SOURCE_MTIME: Optional[float] = None


def _load_prices() -> Dict[str, Tuple[float, float]]:
    global PRICES_SOURCE, PRICES_SOURCE_MTIME
    custom = os.environ.get("AGENTSPEND_PRICING")
    if custom:
        p = Path(custom)
        if p.exists():
            try:
                raw = json.loads(p.read_text())
                table = {k: (float(v[0]), float(v[1])) for k, v in raw.items()}
                PRICES_SOURCE = str(p.resolve())
                try:
                    PRICES_SOURCE_MTIME = p.stat().st_mtime
                except OSError:
                    PRICES_SOURCE_MTIME = None
                return table
            except Exception as exc:
                print(f"[agentspend] AGENTSPEND_PRICING malformed at {p}: "
                      f"{type(exc).__name__}. Falling back to defaults.", flush=True)
    return dict(DEFAULT_PRICES)


PRICES = _load_prices()


def _match_model_key(model: str) -> Optional[str]:
    if not model:
        return None
    m = model.lower().replace(".", "-").replace("_", "-").replace("/", "-")
    if m in PRICES:
        return m
    # Try prefix match for variants like "claude-opus-4-6-20250115"
    for key in PRICES:
        if m.startswith(key) or key.startswith(m[: len(key)]):
            return key
    return None


def _estimate_tokens(tool_input: Any) -> int:
    try:
        s = json.dumps(tool_input) if not isinstance(tool_input, str) else tool_input
    except Exception:
        s = str(tool_input)
    return max(1, int(len(s) / CHARS_PER_TOKEN))


def _compute_cost(model_key: Optional[str], tok_in: int, tok_out: int) -> float:
    if not model_key or model_key not in PRICES:
        return 0.0
    pin, pout = PRICES[model_key]
    return (tok_in / 1_000_000.0) * pin + (tok_out / 1_000_000.0) * pout


# ---------------------------------------------------------------------------
# Local model detection (v0.2)
#
# Local inference runtimes are free at the API layer. We recognise six of them
# explicitly so the dashboard can show a green LOCAL badge instead of a silent
# $0. Unknown paid models flip to "unknown" and surface an amber badge so users
# know to extend AGENTSPEND_PRICING rather than silently under-reporting.
# ---------------------------------------------------------------------------

LOCAL_PROVIDERS = frozenset({
    "ollama", "llama.cpp", "llama_cpp", "llamacpp", "llama-cpp",
    "vllm", "lmstudio", "lm_studio", "lm-studio",
    "tgi", "text-generation-inference",
    "ramalama", "local", "llamafile", "gpt4all", "jan",
})
LOCAL_MODEL_PREFIXES = (
    "ollama/", "local/", "lmstudio/", "llama.cpp/", "llamacpp/",
    "vllm/", "tgi/", "ramalama/", "llamafile/", "jan/",
)
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
LOCAL_PORTS = frozenset({11434, 8080, 8000, 1234, 5000, 8088, 8888, 11435})
_PRIVATE_CIDR = re.compile(
    r"^(?:192\.168\.|10\.|172\.(?:1[6-9]|2\d|3[01])\.|169\.254\.)"
    r"|\.local$|^[^.]+\.home$|^[^.]+\.lan$"
)


def _detect_local(metadata: dict, model: str, source: str) -> Tuple[bool, str]:
    """Return (is_local, runtime_tag). runtime_tag is 'ollama' / 'vllm' / etc
    when detected, else ''. Detection priority: explicit provider tag >
    endpoint URL pattern > model name prefix > source field fallback."""
    provider = str(metadata.get("provider", "")).lower().strip()
    if provider in LOCAL_PROVIDERS:
        return True, provider

    endpoint = (metadata.get("endpoint")
                or metadata.get("api_base")
                or metadata.get("base_url")
                or "")
    if endpoint:
        try:
            parsed = urlparse(str(endpoint).lower())
            host = (parsed.hostname or "").strip()
            port = parsed.port
            if host in LOCAL_HOSTS:
                return True, _runtime_from_port(port) or "local"
            if host and _PRIVATE_CIDR.search(host):
                return True, _runtime_from_port(port) or "local"
            if port in LOCAL_PORTS and not host:
                return True, _runtime_from_port(port) or "local"
        except Exception:
            pass

    mlow = (model or "").lower()
    for prefix in LOCAL_MODEL_PREFIXES:
        if mlow.startswith(prefix):
            return True, prefix.rstrip("/")

    src = (source or "").lower()
    if src in LOCAL_PROVIDERS:
        return True, src

    return False, ""


def _runtime_from_port(port: Optional[int]) -> str:
    if port == 11434:
        return "ollama"
    if port == 1234:
        return "lmstudio"
    if port in (8000,):
        return "vllm"
    if port in (8080, 8088):
        return "llama.cpp"
    if port == 11435:
        return "ollama"
    return ""


def _prices_info() -> dict:
    """Return current effective price table + source metadata for the dashboard."""
    age_days = None
    try:
        from datetime import date
        y, m, d = (int(x) for x in PRICES_BUNDLE_DATE.split("-"))
        age_days = (date.today() - date(y, m, d)).days
    except Exception:
        pass
    return {
        "source": PRICES_SOURCE,  # "defaults" or absolute path
        "model_count": len(PRICES),
        "bundle_date": PRICES_BUNDLE_DATE,
        "bundle_age_days": age_days,
        "override_env": "AGENTSPEND_PRICING",
        "override_active": PRICES_SOURCE != "defaults",
        "prices": {k: list(v) for k, v in sorted(PRICES.items())},
        "local_runtimes": sorted(LOCAL_PROVIDERS),
    }


def _render_help_html(md: str) -> str:
    """Minimal markdown-to-HTML renderer for HELP.md. Avoids a markdown
    library dependency. Handles headings, paragraphs, lists, tables, code
    blocks, bold, italic, inline code, and links, which is everything HELP.md uses."""
    import html as htmllib
    lines = md.split("\n")
    out = []
    in_code = False
    in_table = False
    in_list = False
    table_rows: list = []

    def close_table():
        """Flush any buffered markdown table rows as an HTML <table> block."""
        nonlocal in_table, table_rows
        if not table_rows:
            in_table = False
            return
        out.append('<table>')
        for i, row in enumerate(table_rows):
            cells = [c.strip() for c in row.strip("|").split("|")]
            tag = "th" if i == 0 else "td"
            out.append("<tr>" + "".join(
                f"<{tag}>{_inline_md(c)}</{tag}>" for c in cells) + "</tr>")
        out.append('</table>')
        table_rows = []
        in_table = False

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_table:
                close_table()
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                out.append("<pre><code>")
                in_code = True
            i += 1
            continue
        if in_code:
            out.append(htmllib.escape(line))
            i += 1
            continue
        if stripped.startswith("|") and "|" in stripped[1:]:
            if i + 1 < len(lines) and set(lines[i + 1].strip().replace("|", "").replace("-", "").replace(":", "")) <= {" "}:
                if not in_table:
                    in_table = True
                    table_rows = [stripped]
                    i += 2  # skip the --- separator
                    continue
            if in_table:
                table_rows.append(stripped)
                i += 1
                continue
        if in_table and not stripped.startswith("|"):
            close_table()
        if stripped.startswith("# "):
            out.append(f"<h1>{_inline_md(stripped[2:])}</h1>")
        elif stripped.startswith("## "):
            out.append(f"<h2>{_inline_md(stripped[3:])}</h2>")
        elif stripped.startswith("### "):
            out.append(f"<h3>{_inline_md(stripped[4:])}</h3>")
        elif stripped.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline_md(stripped[2:])}</li>")
            if i + 1 >= len(lines) or not lines[i + 1].strip().startswith("- "):
                out.append("</ul>")
                in_list = False
        elif stripped == "---":
            out.append("<hr>")
        elif stripped:
            out.append(f"<p>{_inline_md(stripped)}</p>")
        i += 1

    if in_table:
        close_table()

    if in_list:
        out.append("</ul>")
    body = "\n".join(out)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>AgentSpend Help</title>
<style>
body{{font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;max-width:820px;margin:40px auto;padding:0 24px;background:#0a0a0a;color:#e0e0e0;line-height:1.65}}
h1,h2,h3{{color:#fff;font-weight:700;margin-top:1.8em}}
h1{{border-bottom:1px solid #2a2a2a;padding-bottom:.3em}}
code{{background:#111;color:#00ff88;padding:2px 6px;border-radius:3px;font-size:.92em}}
pre{{background:#111;padding:16px 20px;border-radius:6px;overflow-x:auto;border:1px solid #2a2a2a}}
pre code{{background:none;padding:0}}
table{{border-collapse:collapse;margin:1em 0;width:100%}}
th,td{{border:1px solid #2a2a2a;padding:8px 12px;text-align:left;vertical-align:top}}
th{{background:#151515;color:#fff}}
a{{color:#00ff88}}
hr{{border:0;border-top:1px solid #2a2a2a;margin:32px 0}}
.back{{display:inline-block;margin-bottom:20px;color:#888;font-size:.85rem}}
</style></head><body>
<a class="back" href="/">&larr; back to dashboard</a>
{body}
</body></html>"""


def _inline_md(text: str) -> str:
    import html as htmllib
    import re as _re
    text = htmllib.escape(text)
    text = _re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = _re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = _re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
    text = _re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def _classify(metadata: dict, model: str, source: str) -> Tuple[str, str, str]:
    """Return (source_class, cost_status, runtime_tag).
    source_class: 'local' | 'cloud'
    cost_status:  'free_local' | 'priced' | 'unknown'
    runtime_tag:  'ollama' | 'vllm' | '' (only when local)
    """
    is_local, runtime = _detect_local(metadata, model, source)
    if is_local:
        return "local", "free_local", runtime
    if _match_model_key(model):
        return "cloud", "priced", ""
    return "cloud", "unknown", ""


class _Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT, ts REAL, event_type TEXT, source TEXT,
                session_id TEXT, tool_name TEXT, model TEXT,
                tok_in INTEGER, tok_out INTEGER, cost_usd REAL,
                estimated INTEGER,
                source_class TEXT DEFAULT '',
                cost_status TEXT DEFAULT '',
                runtime TEXT DEFAULT ''
            )"""
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON events(ts)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON events(session_id)")
        self._migrate_v02()
        self._conn.commit()
        self._purge_old()

    def _migrate_v02(self) -> None:
        """Add v0.2 columns if the DB was created by v0.1."""
        cur = self._conn.execute("PRAGMA table_info(events)")
        existing = {row[1] for row in cur.fetchall()}
        for col in ("source_class", "cost_status", "runtime"):
            if col not in existing:
                try:
                    self._conn.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT DEFAULT ''")
                except sqlite3.OperationalError:
                    pass

    def _purge_old(self) -> None:
        """Delete rows older than RETENTION_DAYS and VACUUM if DB exceeds threshold.
        Runs once at startup. Production deployments should configure a cron.
        Disabled by setting AGENTSPEND_RETENTION_DAYS=0."""
        if RETENTION_DAYS <= 0:
            return
        try:
            with self._lock:
                cutoff = time.time() - (RETENTION_DAYS * 86400)
                cur = self._conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
                deleted = cur.rowcount
                self._conn.commit()
                if deleted:
                    try:
                        size_mb = self._path.stat().st_size / (1024 * 1024)
                    except OSError:
                        size_mb = 0
                    if size_mb > VACUUM_THRESHOLD_MB:
                        self._conn.execute("VACUUM")
                print(f"[agentspend] purged {deleted} rows older than {RETENTION_DAYS}d", flush=True)
        except Exception as exc:
            print(f"[agentspend] retention purge failed: {type(exc).__name__}", flush=True)

    def record(self, row: dict) -> None:
        """Insert a single usage event row into the events table."""
        with self._lock:
            self._conn.execute(
                """INSERT INTO events(event_id, ts, event_type, source, session_id,
                    tool_name, model, tok_in, tok_out, cost_usd, estimated,
                    source_class, cost_status, runtime)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row.get("event_id", ""),
                    row["ts"],
                    row.get("event_type", ""),
                    row.get("source", ""),
                    row.get("session_id", ""),
                    row.get("tool_name", ""),
                    row.get("model") or "",
                    int(row.get("tok_in", 0)),
                    int(row.get("tok_out", 0)),
                    float(row.get("cost_usd", 0.0)),
                    1 if row.get("estimated") else 0,
                    row.get("source_class", ""),
                    row.get("cost_status", ""),
                    row.get("runtime", ""),
                ),
            )
            self._conn.commit()

    def _query(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            cur = self._conn.execute(sql, args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def summary(self) -> dict:
        """Return rolling event, cost, and token totals for today, the last 30 days, and all time, plus the current events-per-minute."""
        now = time.time()
        day_start = now - 86400
        month_start = now - 30 * 86400
        today = self._query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost, "
            "COALESCE(SUM(tok_in),0) AS tin, COALESCE(SUM(tok_out),0) AS tout "
            "FROM events WHERE ts >= ?", (day_start,),
        )[0]
        month = self._query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost, "
            "COALESCE(SUM(tok_in),0) AS tin, COALESCE(SUM(tok_out),0) AS tout "
            "FROM events WHERE ts >= ?", (month_start,),
        )[0]
        all_time = self._query(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS cost "
            "FROM events",
        )[0]
        # events/min over last 60s
        epm = self._query("SELECT COUNT(*) AS n FROM events WHERE ts >= ?", (now - 60,))[0]["n"]
        return {
            "today_events": today["n"], "today_cost": round(today["cost"], 4),
            "today_tokens": today["tin"] + today["tout"],
            "month_events": month["n"], "month_cost": round(month["cost"], 4),
            "month_tokens": month["tin"] + month["tout"],
            "all_events": all_time["n"], "all_cost": round(all_time["cost"], 4),
            "events_per_min": epm,
        }

    def by_model(self, since: float) -> list:
        """Return per-model event counts, token totals, and cost since `since`, ordered by cost (top 12)."""
        return self._query(
            "SELECT COALESCE(NULLIF(model,''),'(unreported)') AS model, "
            "COUNT(*) AS events, SUM(tok_in) AS tok_in, SUM(tok_out) AS tok_out, "
            "SUM(cost_usd) AS cost "
            "FROM events WHERE ts >= ? "
            "GROUP BY model ORDER BY cost DESC LIMIT 12", (since,),
        )

    def by_source(self, since: float) -> list:
        """Return per-source event counts and cost since `since`, ordered by cost (top 12)."""
        return self._query(
            "SELECT COALESCE(NULLIF(source,''),'(unknown)') AS source, "
            "COUNT(*) AS events, SUM(cost_usd) AS cost "
            "FROM events WHERE ts >= ? "
            "GROUP BY source ORDER BY cost DESC LIMIT 12", (since,),
        )

    def by_session(self, since: float) -> list:
        return self._query(
            "SELECT session_id, source, COUNT(*) AS events, SUM(cost_usd) AS cost, "
            "MAX(ts) AS last_seen "
            "FROM events WHERE ts >= ? AND session_id != '' "
            "GROUP BY session_id ORDER BY cost DESC LIMIT 20", (since,),
        )

    def recent(self, limit: int = 50) -> list:
        return self._query(
            "SELECT ts, source, session_id, tool_name, model, tok_in, tok_out, "
            "cost_usd, estimated, "
            "COALESCE(source_class,'') AS source_class, "
            "COALESCE(cost_status,'') AS cost_status, "
            "COALESCE(runtime,'') AS runtime "
            "FROM events ORDER BY ts DESC LIMIT ?", (limit,),
        )


STORE = _Storage(DB_PATH)


def _process_event(envelope: Dict[str, Any]) -> dict:
    """Record one HookBus event, return verdict."""
    ts = time.time()
    event_id = envelope.get("event_id", "")
    event_type = envelope.get("event_type", "")
    source = envelope.get("source", "")
    session_id = envelope.get("session_id", "")
    tool_name = envelope.get("tool_name", "")
    tool_input = envelope.get("tool_input", {})
    metadata = envelope.get("metadata", {}) or {}

    model = metadata.get("model") or metadata.get("model_name") or ""
    tok_in = metadata.get("tokens_input") or metadata.get("prompt_tokens") or 0
    tok_out = metadata.get("tokens_output") or metadata.get("completion_tokens") or 0
    estimated = False

    if not tok_in and not tok_out:
        # Fall back to character-based estimate. Flag as estimated.
        tok_in = _estimate_tokens(tool_input)
        tok_out = max(50, tok_in // 10)  # heuristic: output ~ 10% of input
        estimated = True

    source_class, cost_status, runtime = _classify(metadata, model, source)
    if cost_status == "priced":
        cost_key = _match_model_key(model)
        cost = _compute_cost(cost_key, int(tok_in), int(tok_out))
    else:
        cost = 0.0

    STORE.record({
        "event_id": event_id, "ts": ts, "event_type": event_type,
        "source": source, "session_id": session_id, "tool_name": tool_name,
        "model": model, "tok_in": int(tok_in), "tok_out": int(tok_out),
        "cost_usd": cost, "estimated": estimated,
        "source_class": source_class, "cost_status": cost_status,
        "runtime": runtime,
    })

    return {
        "event_id": event_id,
        "subscriber": "agentspend",
        "decision": "allow",
        "reason": f"[agentspend] logged {int(tok_in)}+{int(tok_out)} tokens"
                  + (" (estimated)" if estimated else "")
                  + (f", ${cost:.4f}" if cost else ""),
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>HookBus-AgentSpend &middot; HookBus™</title>
<style>
:root{--bg:#0b0d10;--panel:#11151a;--line:#1d232b;--text:#e6edf3;--dim:#8b949e;
      --green:#3fb950;--red:#f85149;--amber:#d29922;--blue:#58a6ff;--accent:#f0883e;
      --mono:'SF Mono',Menlo,Consolas,monospace;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font:14px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
header{padding:14px 22px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;background:var(--panel)}
.brand{font-family:var(--mono);font-weight:700;letter-spacing:1px;color:var(--accent)}
.brand span{color:var(--dim);font-weight:400;font-size:.78rem;margin-left:8px}
.licence{font-family:var(--mono);font-size:.72rem;color:var(--dim)}
.licence a{color:var(--blue);text-decoration:none}

.kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;padding:14px 14px 0}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:16px 18px}
.kpi-label{font-family:var(--mono);font-size:.65rem;color:var(--dim);text-transform:uppercase;letter-spacing:1.5px;margin-bottom:8px}
.kpi-value{font-size:1.8rem;font-weight:700;font-family:var(--mono);line-height:1.1}
.kpi-value.cost{color:var(--accent)}
.kpi-sub{font-size:.72rem;color:var(--dim);font-family:var(--mono);margin-top:4px}

.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:14px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:14px}
.panel h2{font-size:.72rem;font-family:var(--mono);text-transform:uppercase;color:var(--dim);letter-spacing:1.5px;margin-bottom:12px;display:flex;justify-content:space-between;align-items:center}
.panel h2 .tag{color:var(--dim);font-weight:400;font-size:.65rem}

table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:.78rem}
th{text-align:left;padding:6px 10px;color:var(--dim);text-transform:uppercase;font-size:.6rem;letter-spacing:1px;border-bottom:1px solid var(--line)}
td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
td.cost{color:var(--accent);font-weight:600}
td.num{text-align:right}
.muted{color:var(--dim)}
.tag-est{background:var(--line);color:var(--dim);padding:1px 5px;border-radius:3px;font-size:.65rem}
.tag-local{background:rgba(0,255,136,0.12);color:#00ff88;padding:1px 6px;border-radius:3px;font-size:.62rem;font-weight:600;letter-spacing:1px;font-family:ui-monospace,monospace}
.tag-unknown{background:rgba(255,200,0,0.12);color:#ffc800;padding:1px 6px;border-radius:3px;font-size:.62rem;font-weight:600;letter-spacing:1px;font-family:ui-monospace,monospace}

.events{grid-column:1 / -1;max-height:520px;overflow-y:auto}
.events table th{position:sticky;top:0;background:var(--panel);z-index:1}
.events tr.new td{animation:flash 1.2s ease-out}
@keyframes flash{from{background:#2a1d0a}to{background:transparent}}

.empty{padding:24px;text-align:center;color:var(--dim);font-style:italic}

footer{padding:10px 22px;border-top:1px solid var(--line);font-family:var(--mono);font-size:.68rem;color:var(--dim);display:flex;justify-content:space-between}
footer a{color:var(--blue);text-decoration:none}

.bar{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin-top:4px}
.bar>div{height:100%;background:var(--accent);transition:width .4s}
</style>
</head>
<body>
<header>
  <div class="brand">AGENTSPEND <span>for HookBus™</span></div>
  <div class="licence">MIT. <a href="https://agenticthinking.uk" target="_blank">Enterprise: per-team budgets + alerts &raquo;</a></div>
</header>

<div id="stale-banner" style="display:none;background:rgba(255,200,0,0.08);border:1px solid rgba(255,200,0,0.3);border-left:3px solid #ffc800;color:#e0e0e0;padding:14px 20px;border-radius:6px;margin-bottom:16px;font-size:0.88rem;line-height:1.5">
  <strong style="color:#ffc800;">Pricing table is <span id="stale-age">N</span> days old.</strong>
  Vendor rates change 2-4 times a year. Verify current prices and override via <code style="background:#111;padding:2px 6px;border-radius:3px;color:#00ff88">AGENTSPEND_PRICING</code> if your rates differ. <a href="/help" style="color:#00ff88">How to override &rarr;</a>
</div>

<div id="pricing-info" style="display:flex;justify-content:space-between;align-items:center;background:rgba(255,255,255,0.02);border:1px solid #2a2a2a;border-radius:6px;padding:10px 16px;margin-bottom:16px;font-size:0.82rem;color:#888">
  <div>
    <span style="color:#e0e0e0;font-weight:600">Pricing</span>:
    <span id="p-count">...</span> models,
    source <code style="background:#111;padding:1px 5px;border-radius:3px;color:#00ff88" id="p-source">...</code>,
    bundle <span id="p-date">...</span>
  </div>
  <div>
    <a href="/prices" target="_blank" style="color:#00ff88;margin-right:14px">View table</a>
    <a href="/help" style="color:#00ff88">Help &amp; override docs</a>
  </div>
</div>

<div class="kpi-row">
  <div class="kpi"><div class="kpi-label">Today</div><div class="kpi-value cost" id="k-today">$0.00</div><div class="kpi-sub" id="k-today-sub">0 events</div></div>
  <div class="kpi"><div class="kpi-label">30 days</div><div class="kpi-value cost" id="k-month">$0.00</div><div class="kpi-sub" id="k-month-sub">0 events</div></div>
  <div class="kpi"><div class="kpi-label">All time</div><div class="kpi-value" id="k-all">$0.00</div><div class="kpi-sub" id="k-all-sub">0 events</div></div>
  <div class="kpi"><div class="kpi-label">Events / min</div><div class="kpi-value" id="k-epm">0</div><div class="kpi-sub">live</div></div>
</div>

<div class="grid">
  <div class="panel">
    <h2>By Model <span class="tag" id="model-window">30 days</span></h2>
    <table><thead><tr><th>Model</th><th class="num">Events</th><th class="num">In</th><th class="num">Out</th><th class="num">Cost</th></tr></thead>
    <tbody id="t-model"><tr><td colspan="5" class="empty">no data yet</td></tr></tbody></table>
  </div>

  <div class="panel">
    <h2>By Publisher <span class="tag" id="source-window">30 days</span></h2>
    <table><thead><tr><th>Source</th><th class="num">Events</th><th class="num">Cost</th></tr></thead>
    <tbody id="t-source"><tr><td colspan="3" class="empty">no data yet</td></tr></tbody></table>
  </div>

  <div class="panel" style="grid-column:1 / -1;">
    <h2>Top Sessions <span class="tag">30 days</span></h2>
    <table><thead><tr><th>Session</th><th>Publisher</th><th class="num">Events</th><th class="num">Cost</th><th>Last seen</th></tr></thead>
    <tbody id="t-session"><tr><td colspan="5" class="empty">no sessions yet</td></tr></tbody></table>
  </div>

  <div class="panel events">
    <h2>Recent events <span class="tag" id="events-count">(live)</span></h2>
    <table><thead><tr><th>Time</th><th>Source</th><th>Tool</th><th>Model</th><th class="num">In</th><th class="num">Out</th><th class="num">Cost</th></tr></thead>
    <tbody id="t-events"><tr><td colspan="7" class="empty">waiting for events...</td></tr></tbody></table>
  </div>
</div>

<footer>
  <span>HookBus™ subscriber v__VERSION__ &middot; MIT &middot; &copy; 2026 Agentic Thinking Ltd</span>
  <span>For per-team budgets + alerts, see Enterprise</span>
</footer>

<script>
const fmtUSD = v => {
  const n = Number(v||0);
  if (n === 0) return '$0.00';
  if (n < 0.01) return '$' + n.toFixed(4);
  if (n < 1) return '$' + n.toFixed(3);
  return '$' + n.toFixed(2);
};
const fmtUSD4 = v => '$' + Number(v||0).toFixed(4);
const fmtNum = v => Number(v||0).toLocaleString();
const fmtTime = ts => new Date(ts*1000).toLocaleTimeString('en-GB',{hour12:false});
const fmtAgo = ts => {
  const s = Math.floor(Date.now()/1000 - ts);
  if(s<60) return s+'s ago';
  if(s<3600) return Math.floor(s/60)+'m ago';
  if(s<86400) return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
};
const esc = s => String(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

let lastId = 0;

async function tick() {
  try {
    // Fetch pricing info once per 60s refresh; updates the info panel + staleness banner
    try {
      const pres = await fetch('/api/prices').then(r=>r.json());
      document.getElementById('p-count').textContent = pres.model_count + (pres.override_active ? ' (override)' : '');
      document.getElementById('p-source').textContent = pres.override_active ? pres.source : 'defaults';
      document.getElementById('p-date').textContent = pres.bundle_date;
      if (!pres.override_active && pres.bundle_age_days !== null && pres.bundle_age_days > 30) {
        document.getElementById('stale-age').textContent = pres.bundle_age_days;
        document.getElementById('stale-banner').style.display = '';
      }
    } catch(e) { /* ignore */ }
    const [sum, byModel, bySource, bySession, recent] = await Promise.all([
      fetch('/api/summary').then(r=>r.json()),
      fetch('/api/by-model').then(r=>r.json()),
      fetch('/api/by-source').then(r=>r.json()),
      fetch('/api/by-session').then(r=>r.json()),
      fetch('/api/recent').then(r=>r.json()),
    ]);

    document.getElementById('k-today').textContent = fmtUSD(sum.today_cost);
    document.getElementById('k-today-sub').textContent = fmtNum(sum.today_events)+' events · '+fmtNum(sum.today_tokens)+' tok';
    document.getElementById('k-month').textContent = fmtUSD(sum.month_cost);
    document.getElementById('k-month-sub').textContent = fmtNum(sum.month_events)+' events · '+fmtNum(sum.month_tokens)+' tok';
    document.getElementById('k-all').textContent = fmtUSD(sum.all_cost);
    document.getElementById('k-all-sub').textContent = fmtNum(sum.all_events)+' events';
    document.getElementById('k-epm').textContent = sum.events_per_min;

    const tbodyModel = document.getElementById('t-model');
    tbodyModel.innerHTML = byModel.length ? byModel.map(r=>
      `<tr><td>${esc(r.model)}</td><td class="num">${fmtNum(r.events)}</td><td class="num muted">${fmtNum(r.tok_in)}</td><td class="num muted">${fmtNum(r.tok_out)}</td><td class="num cost">${fmtUSD4(r.cost)}</td></tr>`
    ).join('') : '<tr><td colspan="5" class="empty">no data yet</td></tr>';

    const tbodySource = document.getElementById('t-source');
    tbodySource.innerHTML = bySource.length ? bySource.map(r=>
      `<tr><td>${esc(r.source)}</td><td class="num">${fmtNum(r.events)}</td><td class="num cost">${fmtUSD4(r.cost)}</td></tr>`
    ).join('') : '<tr><td colspan="3" class="empty">no data yet</td></tr>';

    const tbodySession = document.getElementById('t-session');
    tbodySession.innerHTML = bySession.length ? bySession.map(r=>
      `<tr><td>${esc(r.session_id.slice(0,18))}</td><td>${esc(r.source)}</td><td class="num">${fmtNum(r.events)}</td><td class="num cost">${fmtUSD4(r.cost)}</td><td class="muted">${fmtAgo(r.last_seen)}</td></tr>`
    ).join('') : '<tr><td colspan="5" class="empty">no sessions yet</td></tr>';

    const tbodyEvents = document.getElementById('t-events');
    tbodyEvents.innerHTML = recent.length ? recent.map(e=>{
      const est = e.estimated ? ' <span class="tag-est">est</span>' : '';
      const model = e.model || '<span class="muted">(unreported)</span>';
      let badge = '';
      if (e.cost_status === 'free_local') {
        const rt = e.runtime ? (' ' + esc(e.runtime)) : '';
        badge = ' <span class="tag-local">LOCAL' + rt + '</span>';
      } else if (e.cost_status === 'unknown') {
        badge = ' <span class="tag-unknown">UNKNOWN</span>';
      }
      const costCell = e.cost_status === 'free_local'
        ? '<td class="num" style="color:var(--green)">$0.0000</td>'
        : e.cost_status === 'unknown'
        ? '<td class="num" style="color:var(--accent);opacity:0.6">(no price)</td>'
        : `<td class="num cost">${fmtUSD4(e.cost_usd)}</td>`;
      return `<tr><td>${fmtTime(e.ts)}</td><td>${esc(e.source)}</td><td>${esc(e.tool_name)}</td><td>${model}${est}${badge}</td><td class="num muted">${fmtNum(e.tok_in)}</td><td class="num muted">${fmtNum(e.tok_out)}</td>${costCell}</tr>`;
    }).join('') : '<tr><td colspan="7" class="empty">waiting for events...</td></tr>';

    document.getElementById('events-count').textContent = '(showing '+recent.length+')';
  } catch(e) { /* ignore transient errors */ }
}
tick();
setInterval(tick, 2000);
</script>
</body>
</html>
""".replace("__VERSION__", __version__)


def _prices_page_html() -> str:
    """Human-readable pricing table + instructions for overriding rates."""
    import html as _h
    info = _prices_info()
    rows = []
    for model, (tin, tout) in info["prices"].items():
        free = (tin == 0.0 and tout == 0.0)
        in_cell = "free" if free else f"${tin:.2f}"
        out_cell = "free" if free else f"${tout:.2f}"
        cls = ' class="free"' if free else ""
        rows.append(
            f"<tr{cls}><td><code>{_h.escape(model)}</code></td>"
            f"<td class=\"num\">{in_cell}</td>"
            f"<td class=\"num\">{out_cell}</td></tr>"
        )
    table_html = "\n".join(rows)
    source = info["source"]
    override_active = info["override_active"]
    source_html = (
        f'<code>{_h.escape(source)}</code>' if override_active
        else '<code>built-in defaults</code> '
             '<span class="muted">(no AGENTSPEND_PRICING set)</span>'
    )
    age = info["bundle_age_days"]
    age_html = "" if age is None else f" &middot; bundle is <strong>{age} days</strong> old"
    stale_banner = ""
    if not override_active and age is not None and age > 30:
        stale_banner = (
            '<div class="stale">'
            f'<strong>Pricing table is {age} days old.</strong> '
            'Vendor rates change 2-4 times a year. '
            'Override with your own rates using the steps below.'
            '</div>'
        )
    local_runtimes = ", ".join(info["local_runtimes"]) or "<span class=\"muted\">none</span>"
    return f"""<!doctype html>
<html><head>
<meta charset="utf-8"><title>AGENTSPEND - Model pricing</title>
<style>
:root{{--bg:#0b0d10;--panel:#11151a;--line:#1d232b;--text:#e6edf3;--dim:#8b949e;
      --accent:#f0883e;--green:#3fb950;--mono:'SF Mono',Consolas,Monaco,monospace}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;padding:0 0 40px}}
header{{padding:14px 22px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;background:var(--panel)}}
.brand{{font-family:var(--mono);font-weight:700;letter-spacing:1px;color:var(--accent)}}
.brand span{{color:var(--dim);font-weight:400;margin-left:8px}}
main{{max-width:980px;margin:24px auto;padding:0 22px}}
h1{{font-size:1.4rem;margin-bottom:4px}}
h2{{font-size:1.05rem;margin:28px 0 10px;color:var(--accent)}}
.meta{{color:var(--dim);font-size:.85rem;margin-bottom:18px}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:16px 18px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:.88rem}}
th{{text-align:left;padding:8px 12px;color:var(--dim);text-transform:uppercase;font-size:.65rem;letter-spacing:1px;border-bottom:1px solid var(--line)}}
td{{padding:8px 12px;border-bottom:1px solid var(--line)}}
td.num{{text-align:right;color:var(--accent);font-weight:600}}
tr.free td.num{{color:var(--green)}}
code{{background:#0a0a0a;padding:2px 6px;border-radius:3px;color:var(--green);font-family:var(--mono);font-size:.82rem}}
pre{{background:#070709;border:1px solid var(--line);border-radius:6px;padding:14px 16px;overflow-x:auto;font-family:var(--mono);font-size:.82rem;color:var(--text);line-height:1.55}}
ol{{padding-left:22px;line-height:1.8}}
ol li{{margin-bottom:6px}}
.stale{{background:rgba(255,200,0,.08);border:1px solid rgba(255,200,0,.3);border-left:3px solid #ffc800;color:var(--text);padding:12px 16px;border-radius:6px;margin-bottom:16px;font-size:.88rem}}
.muted{{color:var(--dim)}}
.nav a{{color:var(--green);margin-right:16px;text-decoration:none}}
.nav a:hover{{text-decoration:underline}}
.legend{{color:var(--dim);font-size:.8rem;margin-top:8px}}
</style>
</head><body>
<header>
  <div class="brand">AGENTSPEND <span>for HookBus&trade;</span></div>
  <div class="nav"><a href="/">&larr; Dashboard</a><a href="/help">Help</a><a href="/api/prices" target="_blank">Raw JSON</a></div>
</header>
<main>
  <h1>Model pricing</h1>
  <div class="meta">USD per 1 million tokens &middot; bundle date <strong>{info["bundle_date"]}</strong>{age_html}</div>
  {stale_banner}

  <div class="panel">
    <strong>Active source:</strong> {source_html}<br>
    <strong>Models loaded:</strong> {info["model_count"]}<br>
    <strong>Local runtimes (zero-cost):</strong> {local_runtimes}
  </div>

  <table>
    <thead><tr><th>Model</th><th class="num">Input / 1M tok</th><th class="num">Output / 1M tok</th></tr></thead>
    <tbody>
{table_html}
    </tbody>
  </table>
  <div class="legend">Rows marked <strong style="color:var(--green)">free</strong> are promotional or local-runtime models billed at zero.</div>

  <h2>How to update the prices file</h2>
  <div class="panel">
    <p>AGENTSPEND loads its price table at start-up. You override the built-in defaults by pointing <code>AGENTSPEND_PRICING</code> at a JSON file. Restart the service to pick up changes.</p>

    <ol>
      <li><strong>Create a JSON file</strong> (any path the container/host can read) with one entry per model. Keys are lowercase model IDs, values are <code>[input_usd_per_1m, output_usd_per_1m]</code>:
<pre>{{
  "claude-opus-4-7":   [15.00, 75.00],
  "claude-sonnet-4-6": [ 3.00, 15.00],
  "gpt-5-4":           [ 5.00, 15.00],
  "gemini-3-pro":      [ 1.25,  5.00],
  "glm-5":             [ 0.50,  1.50],
  "minimax-m2-7":      [ 0.30,  1.20]
}}</pre>
      </li>
      <li><strong>Set the environment variable</strong> to the file path, then restart the service:
<pre># docker compose (recommended)
services:
  agentspend:
    environment:
      AGENTSPEND_PRICING: /etc/agentspend/prices.json
    volumes:
      - ./prices.json:/etc/agentspend/prices.json:ro

# systemd unit
Environment=AGENTSPEND_PRICING=/etc/agentspend/prices.json

# bare process
AGENTSPEND_PRICING=/etc/agentspend/prices.json python -m agentspend</pre>
      </li>
      <li><strong>Restart</strong> and reload this page. Active source should switch from <code>built-in defaults</code> to your file path.</li>
    </ol>

    <p class="muted" style="margin-top:12px">Notes: malformed JSON falls back to defaults and logs a warning to stderr. Model IDs are normalised lowercase with <code>.</code>, <code>_</code>, <code>/</code> replaced by <code>-</code>. Pricing changes do not hot-reload &mdash; the service must restart.</p>
  </div>

  <h2>Raw data</h2>
  <div class="panel"><p>The machine-readable version of this table is at <a href="/api/prices" style="color:var(--green)"><code>/api/prices</code></a>. The dashboard polls it every 2s for staleness checks.</p></div>
</main>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        # Quiet default access log; keep only errors via stderr.
        return

    def _check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], _AUTH_TOKEN):
            return True
        from urllib.parse import parse_qs
        q = parse_qs(urlparse(self.path).query)
        if q.get("token", [""])[0] == _AUTH_TOKEN:
            return True
        cookie = self.headers.get("Cookie", "")
        if "hookbus_token=" + _AUTH_TOKEN in cookie:
            return True
        return False

    def _deny_401(self):
        body = b'{"error":"unauthorised","hint":"supply Authorization: Bearer <token> or ?token= query"}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if not self._check_auth():
            return self._deny_401()
        path = urlparse(self.path).path
        now = time.time()
        if path == "/" or path == "/index.html":
            self._send_html(HTML)
        elif path == "/api/summary":
            self._send_json(STORE.summary())
        elif path == "/api/by-model":
            self._send_json(STORE.by_model(now - 30 * 86400))
        elif path == "/api/by-source":
            self._send_json(STORE.by_source(now - 30 * 86400))
        elif path == "/api/by-session":
            self._send_json(STORE.by_session(now - 30 * 86400))
        elif path == "/api/recent":
            self._send_json(STORE.recent(50))
        elif path == "/health":
            self._send_json({"ok": True, "version": __version__})
        elif path == "/api/prices":
            self._send_json(_prices_info())
        elif path == "/prices":
            self._send_html(_prices_page_html())
        elif path == "/help":
            self._send_help()
        else:
            self.send_response(404)
            self.end_headers()

    def _send_help(self) -> None:
        help_path = Path(os.environ.get("AGENTSPEND_HELP", "/app/HELP.md"))
        if not help_path.exists():
            help_path = Path(__file__).parent / "HELP.md"
        try:
            md = help_path.read_text()
        except Exception:
            self.send_response(404)
            self.end_headers()
            return
        html = _render_help_html(md)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        # /event endpoint is also auth-protected, only the bus (with the token) can push
        if not self._check_auth():
            return self._deny_401()
        if urlparse(self.path).path != "/event":
            self.send_response(404)
            self.end_headers()
            return
        event_id = ""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            envelope = json.loads(self.rfile.read(length))
            event_id = envelope.get("event_id", "")
            verdict = _process_event(envelope)
        except Exception:
            logger.exception("agentspend failed to process event %s", event_id)
            verdict = {
                "event_id": event_id, "subscriber": "agentspend",
                "decision": "allow",
                "reason": "internal processing error",
            }
        self._send_json(verdict)


def serve(host: str = HOST, port: int = PORT) -> None:
    banner = (
        f"HookBus-AgentSpend v{__version__} on http://{host}:{port} | "
        f"DB {DB_PATH} | MIT | Free for HookBus publishers."
    )
    print(banner, flush=True)
    ThreadingHTTPServer((host, port), _Handler).serve_forever()


if __name__ == "__main__":
    serve()
