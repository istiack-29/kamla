import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("DISCORD_TOKEN", "x" * 60)
os.environ.setdefault("KAMLA_JOIN_LOG_WEBHOOK_URL", "")
# 1x scale in tests: images are ¼ the pixels (1400×N px rather than 2800×N px), cutting render time by ~4×.
# Layout, font, wrap, and all functional correctness are independent of SCALE; production still uses 2x.
os.environ.setdefault("KAMLA_RENDER_SCALE", "1")
