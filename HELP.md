# AgentSpend Help

Real-time token-cost monitor for autonomous AI agents. Drops in as a HookBus™ subscriber. MIT licence.

This document covers what AgentSpend tracks, how pricing works, how to override it, and what the badges on the dashboard mean.

---

## What AgentSpend tracks

AgentSpend listens for `PostLLMCall` events on HookBus™. Every time an AI agent finishes an LLM call, AgentSpend records:

- **Token counts**, `tokens_input` + `tokens_output` from the publisher shim
- **Model name**, `claude-sonnet-4-6`, `ollama/llama3.1`, `gpt-5-4`, etc.
- **Source**, which publisher fired the event (`hermes`, `openclaw`, `claude-code`, etc.)
- **Session ID**, groups events belonging to the same agent run
- **Cost in USD**, computed from the price table (see below)

Events are persisted to SQLite at `$AGENTSPEND_DB` (default `~/.agentspend/events.db`).

---

## Dashboard badges

Each recent event on the dashboard shows one of three classifications:

| Badge | Meaning | Cost behaviour |
|-------|---------|----------------|
| (none) | Cloud model with a known price | Cost computed from input + output tokens |
| `LOCAL <runtime>` (green) | Detected local inference (Ollama, vLLM, llama.cpp, LM Studio, TGI, Ramalama, llamafile, Jan, GPT4All) | Cost shown as $0.0000 because there is no API bill |
| `UNKNOWN` (amber) | Cloud model whose price is not in the table | Cost shown as `(no price)` so you know to add the model to your override |

The separation matters. A silent `$0.00` on an unknown cloud model would under-report runaway spend, which would be worse than not tracking at all.

---

## Accuracy expectations

| Scenario | Accuracy |
|----------|----------|
| Standard cloud API calls on models in the default table | ±5% |
| Local inference (Ollama etc.) | $0 exactly, there is no API bill |
| Unknown model | Flagged, not silently $0 |
| Anthropic prompt caching enabled | ~10-30% over-reported (cache reads cost 10% of fresh input; we do not track cache tokens separately yet) |
| GPT-5 or Claude extended-thinking reasoning tokens | Under-reported, reasoning tokens are billed but not always in `tokens_output` |
| Anthropic Batch API | Over-reported by 50% (batch price is half the standard rate; not auto-detected) |
| Enterprise-negotiated rates | Not reflected, override the price table with your negotiated rates |

**AgentSpend is not a replacement for your vendor invoice.** Vendor invoices include pre-committed spend, free-tier credits, partnership discounts, volume tiers, and cache-hit ratios that cannot be derived from token counts alone. AgentSpend is the runtime visibility layer the invoice cannot give you: per-agent attribution, anomaly detection, and local-inference tracking.

---

## Pricing: default table

AgentSpend ships with a default price table covering 16 common models (Claude, GPT, Gemini, DeepSeek, GLM, MiniMax, Kimi, Mimo). Prices are in USD per 1 million tokens, expressed as `(input, output)`. Rates are accurate as of April 2026 and are updated with each AgentSpend release.

To see the full default table, hit `GET /api/prices` on the running dashboard, or read `DEFAULT_PRICES` in `__init__.py`.

---

## Overriding the price table

You can replace the entire default table by pointing the `AGENTSPEND_PRICING` environment variable at a JSON file.

### Schema

```json
{
  "model-id-lowercase-dashes": [input_price_per_1M_usd, output_price_per_1M_usd],
  "claude-sonnet-4-6": [3.00, 15.00],
  "gpt-5-4": [5.00, 15.00],
  "gemini-3-pro": [1.25, 5.00],
  "deepseek-v3": [0.27, 1.10],
  "your-negotiated-claude": [2.40, 12.00]
}
```

### Example: set up a custom table

```bash
# 1. Create the file
cat > ~/.agentspend/prices.json <<'EOF'
{
  "claude-sonnet-4-6": [2.40, 12.00],
  "claude-haiku-4-5": [0.64, 3.20],
  "gpt-5-4": [4.00, 12.00],
  "gemini-3-pro": [1.00, 4.00],
  "internal-model": [0.10, 0.40]
}
EOF

# 2. Mount into the AgentSpend container
#    (edit your docker-compose.yml)
services:
  agentspend:
    environment:
      AGENTSPEND_PRICING: /config/prices.json
    volumes:
      - ~/.agentspend/prices.json:/config/prices.json:ro

# 3. Restart
docker compose up -d agentspend
```

Your custom file **replaces** the default table entirely. Include every model you want priced. Any model not in your file will show as `UNKNOWN` on the dashboard.

If your JSON is malformed, AgentSpend falls back to the built-in defaults and logs a warning.

---

## Local model detection

AgentSpend recognises the following local inference runtimes automatically. No configuration needed, but the publisher shim can set `metadata.provider` explicitly for highest confidence.

### Detection signals (priority order)

1. **Explicit `metadata.provider` tag** in the envelope, most reliable
2. **Endpoint URL pattern**, `localhost`, `127.0.0.1`, private CIDRs (`192.168.*`, `10.*`, `172.16-31.*`), `.local` domains, plus known local ports (`11434`, `8080`, `8000`, `1234`, `8088`, `11435`)
3. **Model name prefix**, `ollama/`, `local/`, `lmstudio/`, `llama.cpp/`, `vllm/`, `tgi/`, `ramalama/`, `llamafile/`, `jan/`

### Recognised runtimes

| Runtime | Default port | Provider tag |
|---------|--------------|--------------|
| Ollama | 11434 | `ollama` |
| llama.cpp / llama-server | 8080, 8088 | `llama.cpp` or `llamacpp` |
| vLLM | 8000 | `vllm` |
| LM Studio | 1234 | `lmstudio` or `lm_studio` |
| Text Generation Inference (Hugging Face) | 8080 | `tgi` or `text-generation-inference` |
| Ramalama (Red Hat) | any | `ramalama` |
| Llamafile | any | `llamafile` |
| Jan | any | `jan` |
| GPT4All | any | `gpt4all` |
| Generic / unspecified local | any | `local` |

If your local runtime is not on this list, set `metadata.provider: "local"` in the envelope and AgentSpend will classify as `free_local` with runtime tag `local`.

---

## API endpoints

All endpoints require a valid HookBus™ bearer token (`Authorization: Bearer $TOKEN`, or `?token=` query string, or `hookbus_token` cookie).

| Endpoint | Purpose |
|----------|---------|
| `GET /` | Dashboard HTML |
| `GET /api/stats` | Today / month / all-time totals |
| `GET /api/by-model?since=<unix-ts>` | Per-model breakdown |
| `GET /api/by-source?since=<unix-ts>` | Per-publisher breakdown |
| `GET /api/by-session?since=<unix-ts>` | Per-session breakdown |
| `GET /api/recent` | Last 50 events |
| `GET /api/prices` | Current price table + source (default or override path) |
| `GET /help` | This help document, rendered as HTML |
| `POST /event` | Publisher posts a HookBus™ envelope here |

---

## Configuration reference

| Environment variable | Default | Purpose |
|---------------------|---------|---------|
| `HOOKBUS_TOKEN` | (reads from mounted file) | Bearer token. Required. |
| `HOOKBUS_TOKEN_PATH` | `/root/.hookbus/.token` | Path to the shared token file |
| `AGENTSPEND_DB` | `~/.agentspend/events.db` | SQLite file path |
| `AGENTSPEND_PRICING` | (unset, uses defaults) | Path to a custom JSON price table |
| `AGENTSPEND_RETENTION_DAYS` | `90` | How long to keep events. `0` disables purging. |
| `AGENTSPEND_VACUUM_MB` | `128` | Size threshold that triggers SQLite VACUUM after a retention purge |
| `AGENTSPEND_PORT` | `8879` | Dashboard + API port inside the container |
| `AGENTSPEND_HOST` | `0.0.0.0` | Bind address |

---

## Troubleshooting

**Dashboard shows all events as `UNKNOWN`.**
Your publisher shims are reporting model names that are not in the default price table. Either upgrade AgentSpend to a version with your model, or override with `AGENTSPEND_PRICING`.

**Events show $0.00 but I am paying real money.**
Check the classification. A green `LOCAL` badge means AgentSpend thinks this is local inference. If you are actually hitting a cloud API, your publisher shim is likely misreporting the provider or endpoint. Set `metadata.provider: "anthropic"` (or whatever cloud vendor) explicitly in the shim.

**Cost is 10-30% higher than my actual Anthropic bill.**
Probably prompt caching. Cache-read tokens cost 10% of standard input tokens at Anthropic; AgentSpend does not yet split cache tokens. Planned for v0.3.

**Dashboard empty after restart.**
Events database is at `$AGENTSPEND_DB`. Check the path is mounted as a volume so data persists across restarts. Default `~/.agentspend/events.db` inside the container is lost on rebuild unless you mount a host path.

**Cannot override the price table.**
Set `AGENTSPEND_PRICING=/path/to/prices.json` as an environment variable on the AgentSpend container, and mount the file read-only at that path. Restart the container. Check `GET /api/prices` to confirm the source.

---

## Reporting issues

File an issue at https://github.com/agentic-thinking/hookbus-agentspend with:
- AgentSpend version (footer of the dashboard)
- Publisher shim and version
- The event envelope that behaves unexpectedly (redact any secrets first)
- What you expected vs what happened

Licence: MIT. Copyright 2026 Agentic Thinking Ltd.
