"""Board Atlas — profile-agnostic ATS board registry + freshness-first
incremental poller (v2 Phase 2). See spec §7.1/§7.2/§7.5.

No daemon: the TICK (tick.py) is idempotent/resumable and runs on cron or
on-wake, or as the 'atlas' discover sub-source. All live HTTP is politeness-
gated (politeness.py); tests inject httpx.MockTransport + a fake clock."""
from __future__ import annotations

ATS_TYPES = ("greenhouse", "lever", "ashby")
USER_AGENT = "Mozilla/5.0 (compatible; ApplyPilotBot/1.0)"
