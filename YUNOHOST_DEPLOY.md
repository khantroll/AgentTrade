# YunoHost Deployment Guide

This guide deploys the trading agent on a YunoHost server as:
- **Cron jobs** for trading cycles (4×/day on weekdays)
- **My Webapp** for the static dashboard (auto-refreshes every cycle)
- **systemd service** for the config server (so settings.html works persistently)

---

## Directory layout

```
/opt/trading-agent/          ← Python app (private, not web-accessible)
  agent.py
  screener.py
  buckets.py
  llm_router.py
  config_server.py
  requirements.txt
  .env
  config.json                ← created by settings.html
  agent_state.json           ← written after every cycle
  token_usage.json
  bucket_tags.json
  trading_agent.log
  cron.log
  .venv/

/var/www/my_webapp/www/      ← web-accessible (served by nginx via My Webapp)
  index.html                 ← dashboard.html
  settings.html
  agent_state.json           ← copied from /opt after each cycle
```

---

## Step 1 — Install My Webapp in YunoHost

1. Go to **YunoHost admin → Apps → Install**
2. Search for **My Webapp** and install it
3. Set path to `/dashboard` (or whatever URL you want)
4. Set it to **private** (requires YunoHost login) — your keys are displayed here
5. Note the web directory: `/var/www/my_webapp/www/`

---

## Step 2 — Copy files to the server

```bash
# Create app directory
sudo mkdir -p /opt/trading-agent

# Copy Python files
sudo cp agent.py screener.py buckets.py llm_router.py \
        config_server.py requirements.txt /opt/trading-agent/

# Copy deploy scripts
sudo cp deploy/run_cycle.sh deploy/monitor.sh deploy/midnight_reset.sh \
        /opt/trading-agent/
sudo chmod +x /opt/trading-agent/*.sh

# Copy web files
sudo cp dashboard.html /var/www/my_webapp/www/index.html
sudo cp settings.html  /var/www/my_webapp/www/settings.html
```

---

## Step 3 — Create the Python virtual environment

```bash
cd /opt/trading-agent
sudo python3 -m venv .venv
sudo .venv/bin/pip install --upgrade pip
sudo .venv/bin/pip install -r requirements.txt
```

---

## Step 4 — Configure API keys

**Option A — Settings UI (recommended):**
1. Start the config server temporarily:
   ```bash
   cd /opt/trading-agent && sudo .venv/bin/python config_server.py &
   ```
2. Open `https://yourdomain.com/dashboard/settings.html` in your browser
3. Enter all your API keys and click Save All
4. Kill the temporary server (`kill %1`) — systemd will manage it from now on

**Option B — Edit .env directly:**
```bash
sudo cp /opt/trading-agent/.env.example /opt/trading-agent/.env
sudo nano /opt/trading-agent/.env
```
Fill in at minimum:
```
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true          # ← keep true until you fully trust the agent
ANTHROPIC_API_KEY=sk-ant-...   # or OPENAI_API_KEY / GEMINI_API_KEY / MISTRAL_API_KEY / DEEPSEEK_API_KEY / GROQ_API_KEY
LLM_MODE=tiered
DAILY_TOKEN_BUDGET=200000

# Optional. Leave blank to keep today's Groq/Gemini/Mistral list unchanged.
# A real key is enough; do not commit the key. Restart the agent after saving.
OPENROUTER_API_KEY=
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=openrouter/free
NVIDIA_API_KEY=
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
# Override if you do not want the built-in current NIM model.
NVIDIA_LLAMA_MODEL=nvidia/nemotron-3-super-120b-a12b
LLM_TIERED_AUTO_FAILOVER=1
```

---

## Step 5 — Install the config server as a systemd service

```bash
sudo cp /opt/trading-agent/deploy/trading-agent-config.service \
        /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable trading-agent-config
sudo systemctl start trading-agent-config

# Verify it's running
sudo systemctl status trading-agent-config
```

The config server runs at `http://127.0.0.1:5111` (localhost only).
Your settings.html talks to it through the browser on the same machine.

---

## Step 6 — Add cron jobs

```bash
sudo crontab -e
```

Paste these lines:

```cron
# ── AgentTrade — weekdays only ──────────────────────────────────────────────

# Trading cycles (US market hours, ET — adjust if your server is in UTC)
35  9  * * 1-5  /opt/trading-agent/run_cycle.sh  >> /opt/trading-agent/cron.log 2>&1
30 11  * * 1-5  /opt/trading-agent/run_cycle.sh  >> /opt/trading-agent/cron.log 2>&1
30 13  * * 1-5  /opt/trading-agent/run_cycle.sh  >> /opt/trading-agent/cron.log 2>&1
 0 15  * * 1-5  /opt/trading-agent/run_cycle.sh  >> /opt/trading-agent/cron.log 2>&1

# Position monitor every 30 min during market hours
*/30 9-15 * * 1-5  /opt/trading-agent/monitor.sh >> /opt/trading-agent/cron.log 2>&1

# Midnight reset — clears daily trade counter and token budget
0 0 * * *  /opt/trading-agent/midnight_reset.sh  >> /opt/trading-agent/cron.log 2>&1
```

> **Timezone note:** YunoHost servers are often in UTC. US market opens at
> 14:30 UTC (09:30 ET) in winter, 13:30 UTC (09:30 ET) in summer (DST).
> Check your server timezone with `timedatectl` and adjust times accordingly.
> Or force ET in crontab: add `TZ=America/New_York` as the first line.

---

## Step 7 — Test a single run

```bash
cd /opt/trading-agent
sudo bash run_cycle.sh
```

Watch the output. If it completes without errors, check:
```bash
# Agent state written?
ls -la /opt/trading-agent/agent_state.json

# Published to web?
ls -la /var/www/my_webapp/www/agent_state.json

# Dashboard loading at your domain?
# Open https://yourdomain.com/dashboard/
```

---

## Checking logs

```bash
# Cron output
tail -f /opt/trading-agent/cron.log

# Agent detail log
tail -f /opt/trading-agent/trading_agent.log

# Config server
sudo journalctl -u trading-agent-config -f
```

---

## Deploy SHA

`DEPLOY_SHA.txt` is the only commit a reader should trust. `DEPLOY_SHA` (no extension) is a mirror of that same text. They used to be written separately, so a deploy could update the `.txt` file and leave the extensionless file on the previous commit.

`update_deploy.sh` and `install.sh` call `write_deploy_sha.sh`, which writes both files from one value:

1. `DEPLOY_SHA` in the environment, if you pass one (`sudo DEPLOY_SHA=60a1dc10 bash update_deploy.sh -y`)
2. otherwise `git rev-parse --short=8 HEAD` of the checkout you are deploying

`run_cycle.sh` does not move that stamp when git HEAD changes. If the two files disagree, it rewrites both from `DEPLOY_SHA.txt` and copies that one value into the web directory as both names. Do not hand-edit only one file. If the app directory is not a git checkout, set `DEPLOY_SHA` for the deploy command, or run `bash write_deploy_sha.sh /opt/trading-agent <sha>`.

## Updating files after code changes

```bash
sudo cp agent.py screener.py buckets.py llm_router.py \
        config_server.py /opt/trading-agent/

sudo cp dashboard.html /var/www/my_webapp/www/index.html
sudo cp settings.html  /var/www/my_webapp/www/settings.html

sudo systemctl restart trading-agent-config
```

---

## Differences from local development mode

| Feature | Local (python agent.py) | YunoHost (cron) |
|---|---|---|
| Scheduling | `schedule` library loop | OS cron |
| Dashboard data | Manual file upload | Auto-fetched from web directory |
| `daily_trades` counter | In-memory (resets on restart) | Persisted in `agent_state.json` |
| Config server | Started by `agent.py` | Separate systemd service |
| Overlap protection | Single process | `flock` file lock in `run_cycle.sh` |
| Log rotation | Manual | Auto in `run_cycle.sh` at 10MB |

---

## Security checklist

- [ ] My Webapp is set to **private** (requires YunoHost SSO login)
- [ ] `ALPACA_PAPER=true` until you've run paper mode for several weeks
- [ ] `config.json` and `.env` are in `/opt/trading-agent/` (not web-accessible)
- [ ] Config server binds to `127.0.0.1` only (enforced in `config_server.py`)
- [ ] Add `config.json`, `.env`, `agent_state.json`, `token_usage.json`,
  `bucket_tags.json` to `.gitignore` before any git operations
