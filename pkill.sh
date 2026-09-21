sudo pkill -f "agent.py run"
cd /opt/trading-agent
set -a
source .env
set +a
/opt/trading-agent/.venv/bin/python agent.py run