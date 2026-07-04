"""Form Compiler apply engine v2 (spec §6). Greenhouse-only in Phase 3.

Four stages over one shared FormSchema IR: Parse (frontend_greenhouse) ->
Resolve (resolver) -> Fill (executor + drivers) -> Verify (verify). The
Operator (operator.py) is the only AI in the loop. The safety kernel
(submit_broker / submission_ledger / browser_stream._guard) is REUSED, never
rebuilt — the orchestrator threads the SAME objects worker_loop constructs.
v2 fails OPEN: any failure -> sentinel -> legacy run_job. Behind
APPLYPILOT_V2_ENGINE; cutover only at v2 >= v1 on 100+ live rows."""
from __future__ import annotations

V2_ENGINE_ENV = "APPLYPILOT_V2_ENGINE"
V2_TIER_LABEL = "v2_greenhouse"          # written to prefill_status["tier_used"]
