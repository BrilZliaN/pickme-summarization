# pickme-summarization

Telegram bot that tracks group-chat messages and provides LLM-powered summarization, per-user memory, user evaluation, and grounded Q&A — including natural-language commands — via the free Hetzner Experiments Inference API with failover to OpenCode Zen (free) and OpenCode Go.

## Features

- **Message tracking** — every group message is ingested to SQLite (WAL) with media metadata.
- **Summarization** — `/summarize [N]` plus natural-language ("summarize last 50") with map-reduce for large windows.
- **Per-user memory** — batched incremental `user_profiles` merged by LLM every 50 messages or 6 h TTL, with decay.
- **Evaluation** — `/evaluate [@user | me | everyone]` renders rubric-based Markdown cards.
- **Grounded Q&A** — `/ask` / mention / reply-to-bot with rolling summary + profile + recent-window context.
- **Natural-language intent routing** — keyword fast-path + LLM JSON intent classifier (defaults to Q&A).
- **Three-tier LLM failover** — OpenCode Zen free (primary, multi-model selection) → Hetzner (free) → OpenCode Go (paid), with token-bucket rate limiting, immediate 429 model/provider failover, and circuit breakers.

## ⚠️ CRITICAL: Privacy Mode

The bot **must** be able to read all group messages to track the chat.

In **@BotFather**, run:

```
/setprivacy → select your bot → Disable
```

Alternatively, make the bot an **admin** in every group where it tracks messages.

If privacy mode stays **Enabled** and the bot is not an admin, it will only see commands and mentions — tracking will be **silently incomplete** and summaries / evaluations will have gaps. This is also noted in `/start` and `/help`.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(required)* | Bot token from @BotFather via `/newbot` |
| `HETZNER_API_KEY` | `""` | Hetzner Inference API key — via Hetzner Cloud Console > API |
| `HETZNER_BASE_URL` | `https://inference.hetzner.com/api/v1` | Hetzner Inference base URL |
| `LLM_PRIMARY_MODEL` | `Qwen/Qwen3.6-35B-A3B-FP8` | Primary Hetzner model |
| `LLM_FALLBACK_MODEL` | `Qwen3.8-27B` | In-tier fallback model |
| `OPENCODE_API_KEY` | `""` | Single key for Zen + Go — via https://opencode.ai/auth |
| `ZEN_BASE_URL` | `https://opencode.ai/zen/v1` | OpenCode Zen base URL |
| `ZEN_FREE_MODEL` | `space-bunny-free` | Zen free model (used when `ZEN_FREE_MODELS` is empty) |
| `ZEN_FREE_MODELS` | `space-bunny-free,big-pickle,mimo-v2.6-flash-free` | CSV preference list of Zen free models; first match in the live `/models` listing wins, else a random `-free` model. Explicit entries may include unsuffixed free models (e.g. `big-pickle`) |
| `GO_BASE_URL` | `https://opencode.ai/zen/go/v1` | OpenCode Go base URL |
| `GO_MODEL` | `mimo-v2.5` | Go model |
| `GO_ENABLED` | `1` | Enable Go tier (`0` to disable) |
| `MEMORY_BATCH_SIZE` | `50` | Messages per user before a profile merge is queued |
| `MEMORY_TTL_HOURS` | `6` | Max age before a profile merge is forced |
| `SUMMARIZE_DEFAULT` | `100` | Default message count for `/summarize` |
| `SUMMARIZE_CAP` | `500` | Maximum message count for `/summarize` |
| `ROUTER_FASTPATH` | `1` | Enable keyword fast-path in intent router |
| `RATE_LIMIT_PER_60S` | `8` | Token-bucket rate limit (requests per 60 s) |
| `LLM_CONCURRENCY` | `2` | Max concurrent LLM requests (semaphore) |
| `LOG_LEVEL` | `INFO` | Log level |
| `DATA_DIR` | `data` (`/app/data` in Docker) | Directory for `pickme.db` (WAL) |

Copy `.env.example` to `.env` and fill the required keys.

## Local Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env   # then edit .env
python -m pickme.main
```

Python 3.12+ required.

## Docker

```powershell
Copy-Item .env.example .env   # edit .env
docker compose up --build -d
docker compose logs -f bot
```

The SQLite database (including WAL files) persists in `./data:/app/data`. The container is limited to 512 MiB.

**Linux hosts (VPS):** the container runs as non-root user `app` (uid 1000). The bind-mounted `./data` directory must be writable by that uid, otherwise the bot crash-loops with `sqlite3.OperationalError: unable to open database file`:

```bash
mkdir -p data && chown -R 1000:1000 data   # run before first `docker compose up`
```

Re-run `chown -R 1000:1000 data` whenever root-owned files land in `data/` (e.g. after scp'ing a database as root).

## Commands

| Trigger | Args | Behavior |
|---|---|---|
| `/start`, `/help` | — | Capabilities + privacy note |
| `/summarize` | `[X]` (default 100, cap 500) | Summarize last X messages; map-reduce if large |
| `/evaluate` | `[@user \| me \| everyone]` | Markdown assessment card |
| `/ask` | `<question>` | Q&A grounded in context + memories |
| **NL address** (mention / reply-to-bot) | free text | Intent-routed: "summarize last messages for me", "what do you think of @dan?", "оцени всех" |
| `/forget` | `[me \| @user \| chat]` | Delete that user's data / whole chat |
| `/status` | (admin) | Active provider tier, model, health, queue depth, 429 count |

Replies use HTML parse mode and are split at 4096 characters.

## Data & Privacy

- **Third-party processing:** Message text is sent to Hetzner Inference and (on failover) to OpenCode Zen / Go. There is **no DPA** with these providers. Treat this bot as a **prototype / internal tool** — do not use it for sensitive or regulated data.
- **Aliasing:** Real display names are replaced with stable aliases (`user_123`) in all LLM prompts. The `user_id → display_name` map stays local and is only applied to the final Telegram reply.
- **Deletion:** `/forget me` deletes your profile and messages in that chat, `/forget @user` (admin) deletes a user's data, `/forget chat` (admin) deletes the whole chat. Data lives only in the local SQLite file under `DATA_DIR`.

## Architecture

See [bot-plan.md](bot-plan.md) for the full architecture plan, schema, pipelines, LLM client design, deployment, and risks.
