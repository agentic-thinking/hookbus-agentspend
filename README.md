# HookBus-AgentSpend

**Free HookBus subscriber that tracks AI agent token usage and cost.**

Built for the OpenClaw cost-refugee crisis (April 2026). Works with any HookBus publisher: OpenClaw, Hermes, Claude Code, Cursor, Amp, OpenAI Agents SDK, Anthropic Agent SDK.

## Install (60 seconds)

One shell command installs HookBus-AgentSpend as part of the HookBus stack, together with the bus and CRE-AgentProtect. Interactive menu lets you pick a publisher shim for your agent runtime.

```bash
curl -fsSL https://agenticthinking.uk/install.sh | bash
```

Non-interactive variants:

```bash
# Hermes-agent users
curl -fsSL https://agenticthinking.uk/install.sh | bash -s -- --runtime hermes

# OpenClaw users
curl -fsSL https://agenticthinking.uk/install.sh | bash -s -- --runtime openclaw

# Bus + subscribers only, skip publisher
curl -fsSL https://agenticthinking.uk/install.sh | bash -s -- --runtime skip --noninteractive
```

The script prints the dashboard URL + bearer token on completion. Re-run any time, it is idempotent.

_Prefer not to pipe curl to bash? Inspect first:_ `curl -fsSL https://agenticthinking.uk/install.sh > install.sh && less install.sh && bash install.sh`

---

## Manual install

If you prefer to see every step, or you are building an immutable / reproducible deployment, here is the full manual install.

HookBus-AgentSpend is a HookBus subscriber. The easiest way to run it is via the HookBus quickstart, which builds this image alongside the bus and wires it as a subscriber:

```bash
# Follow the HookBus quickstart:
#   https://github.com/agentic-thinking/hookbus#quick-start-60-seconds
```

**Build the image on its own** to wire AgentSpend into an existing HookBus deployment:

```bash
git clone https://github.com/agentic-thinking/hookbus-agentspend.git
cd hookbus-agentspend
docker build -t agentic-thinking/hookbus-agentspend:latest .

docker run -d --name agentspend \
  -v hookbus-auth:/root/.hookbus:ro \
  -v agentspend-data:/root/.agentspend \
  -p 8883:8879 \
  agentic-thinking/hookbus-agentspend:latest
```

Then add it to `subscribers.yaml`:

```yaml
- name: agentspend
  type: async
  transport: http
  address: http://agentspend:8879/event
  events: [PreToolUse, PostToolUse, PostLLMCall]
  metadata:
    ui_port: 8883
```
- name: agentspend
  type: async
  transport: http
  endpoint: http://agentspend:8879/event
  events: [PreToolUse, PostToolUse]
EOF
kill -HUP $(pidof hookbus)

# 4. Open the dashboard
open http://localhost:8879
```

## What it does

- Records every HookBus event with its source, session, tool, model, and tokens
- If the publisher reports `tokens_input` + `tokens_output` + `model` in event `metadata`, HookBus-AgentSpend uses exact figures
- If the publisher does not report usage, HookBus-AgentSpend estimates from tool-input length (flagged as "est" in the UI)
- Calculates USD cost per event using a bundled model price table (override via `AGENTSPEND_PRICING` env var)
- Serves a live dashboard at `/` with:
  - Today / 30-day / all-time spend KPIs
  - By-model breakdown (events, tokens, cost)
  - By-publisher breakdown
  - Top sessions
  - Live event stream

## Config

| Env var | Default | Purpose |
|---|---|---|
| `AGENTSPEND_HOST` | `0.0.0.0` | Bind address |
| `AGENTSPEND_PORT` | `8879` | HTTP port |
| `AGENTSPEND_DB` | `~/.agentspend/events.db` | SQLite path |
| `AGENTSPEND_PRICING` | (bundled table) | Path to JSON file overriding model prices |

Example `pricing.json` override:

```json
{
  "claude-opus-4-7": [15.0, 75.0],
  "our-custom-model": [0.5, 1.5]
}
```

Prices are in USD per 1M tokens, `[input, output]`.

## Scope

HookBus-AgentSpend is deliberately simple. Install it, watch your spend, catch cost surprises before they happen.

Need enterprise-grade policy, audit, DLP, and memory? See [HookBus Enterprise](https://agenticthinking.uk) for the commercial tier.

## Docs and pricing overrides

Full documentation, accuracy caveats, pricing override schema, and the list of
recognised local inference runtimes are in [`HELP.md`](./HELP.md).

Useful endpoints (all require the shared HookBus bearer token):

| Endpoint | Purpose |
|----------|---------|
| `GET /` | Dashboard HTML |
| `GET /help` | `HELP.md` rendered as HTML |
| `GET /api/prices` | Current effective price table + source (defaults or override path) + bundle age |
| `GET /api/summary` | Today / 30 days / all-time totals |
| `GET /api/recent` | Last 50 events with cost classification |

### Three-state cost classification

Every event is tagged with one of:

- **`priced`** , model in the default or override price table, cost computed from input + output tokens
- **`free_local`** , local inference runtime detected (Ollama, vLLM, llama.cpp, LM Studio, TGI, Ramalama, Llamafile, Jan, GPT4All, or generic `local`). Cost shown as `$0` because there is no API bill.
- **`unknown`** , cloud model name not in the price table. Cost shown as `(no price)` with an amber badge so you know to extend `AGENTSPEND_PRICING`.

The separation exists because a silent `$0` on an unknown cloud model would
under-report runaway spend, which would be worse than not tracking at all.

### Overriding the price table

Point `AGENTSPEND_PRICING` at a JSON file to replace the default 16-model table:

```bash
cat > ~/.agentspend/prices.json <<'EOF'
{
  "claude-sonnet-4-6": [2.40, 12.00],
  "internal-model":    [0.10, 0.40]
}
EOF
export AGENTSPEND_PRICING=~/.agentspend/prices.json
```

Mount the file into the container and restart. See [`HELP.md`](./HELP.md) for
the full schema and docker-compose example.

---

## Licence

MIT. Copyright 2026 Agentic Thinking Ltd.

---

**Part of the HookBus ecosystem.** HookBus is a vendor-neutral lifecycle event bus for AI agents (UK patent GB2608069.7 pending). HookBus-AgentSpend is one of several reference subscribers alongside CRE-AgentProtect (AGT policy adapter). See [HookBus Enterprise](https://agenticthinking.uk) for the commercial tier.
