import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("DISCORD_TOKEN", "x" * 60)
os.environ.setdefault("KAMLA_JOIN_LOG_WEBHOOK_URL", "")
