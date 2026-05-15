# Claude Haiku 4.5 vs Sonnet 4.6 for the apply step

User asked: can we use Haiku instead of Sonnet for the auto-apply step?

## Why this is even worth asking

After iter1+2+3 fixes, the `applypilot apply` Sonnet driver reaches `applied` in ~115s mean on Greenhouse score-8 jobs (vs ~300s wall-clock timeout we kept hitting before). With reliability solved, model cost becomes the next lever.

Haiku 4.5 is **66% cheaper** than Sonnet 4.6 on both input and output tokens:

| | Input ($/MTok) | Output ($/MTok) |
|---|---|---|
| Sonnet 4.6 | $3 | $15 |
| Haiku 4.5 | $1 | $5 |

A typical post-iter3 apply consumes roughly 50k input + 20k output tokens → ~$0.45 with Sonnet, ~$0.15 with Haiku. The user is on Claude Max 5x ($100/mo flat) so this is theoretical, but useful for any future export of the pipeline.

Caveat from claude-code-guide research: Haiku has a 200k context window vs Sonnet's 1M. For long sessions with accumulated DOM snapshots this could matter. For a typical 3-min apply, no.

## How to switch

Already wired. `cli.py:172` exposes `--model` / `-m` (default `"sonnet"`). Pass through to Claude Code CLI argv at `launcher.py:869`. No code change needed.

```powershell
# Switch a single run to Haiku
applypilot apply --workers 1 --limit 1 --model claude-haiku-4-5-20251001
```

To make it the default, change `cli.py:172` default to `"claude-haiku-4-5-20251001"` (or alias `"haiku"` if Claude Code recognizes it).

## A/B benchmark — 3 jobs each, identical pool of Figma score-8 Greenhouse jobs

### Sonnet 4.6 baseline (from iter3 validation, same day)

| Job | Status | Duration | Prefill | Cost (theoretical) |
|---|---|---|---|---|
| Designer Advocate – Figma Weave (NY) | dry_run:applied | 65s | 13 | ~$0.30 |
| Product Designer, AI Models | dry_run:applied | 93s | 17 | ~$0.40 |
| Manager, Software Eng – Interaction Design | dry_run:applied | 132s | 17 | ~$0.50 |
| Manager, Product Design | dry_run:applied | 140s | 17 | ~$0.50 |
| Designer Advocate, Federal | dry_run:applied | 145s | 17 | ~$0.55 |
| **Sonnet aggregate (5 dry-runs)** | **5/5 = 100%** | **mean 115s, p50 132s** | — | — |
| Live: Designer Advocate Figma Weave (NY) | applied | 139s | 13 | $0.49 (actual from console) |

### Haiku 4.5 results (3 dry-runs, same Figma score-8 jobs)

| Job | Status | Duration | Prefill |
|---|---|---|---|
| Product Designer, AI Models | dry_run:applied | 172s | 17 |
| Manager, Software Eng – Interaction Design | dry_run:applied | 110s | 17 |
| Manager, Product Design | dry_run:applied | 67s | 17 |
| **Haiku aggregate (3 dry-runs)** | **3/3 = 100%** | **mean 116s, p50 110s** | — |

**Total console cost across 3 Haiku dry-runs: $0.672 → $0.224/job.**
Sonnet's live submission was $0.49/job per console — Haiku was ~55% cheaper in practice.

### Side-by-side (3 jobs ran on both models)

| Job | Sonnet duration | Haiku duration |
|---|---|---|
| Product Designer, AI Models | 93s | 172s |
| Manager, Software Eng – Interaction Design | 132s | 110s |
| Manager, Product Design | 140s | 67s |
| **Mean** | **122s** | **116s** |

Haiku is fractionally faster on average and never timed out. Both models reach `dry_run:applied` reliably on this form set.

## Recommendation

**Adopt Haiku 4.5 as the default for the apply step.** On the 3-job overlap, Haiku matched Sonnet's success rate (3/3 = 100%) at ~half the cost and equivalent speed.

Switch by patching `cli.py:172`:

```python
model: str = typer.Option("claude-haiku-4-5-20251001", "--model", "-m", help="Claude model name."),
```

Caveats / when to fall back to Sonnet:
- Forms with very long descriptions or many custom screening questions may approach Haiku's 200k context vs Sonnet's 1M. None of the 3 benchmark forms got close, but flag if you see `rate_limited:claude_usage_limit` or context-related errors in `review.jsonl`.
- For an unknown new ATS (Lever, Ashby, Workday) where the prompt's STEP-BY-STEP may not generalize, Sonnet's larger context + stronger reasoning could be worth the premium. Use `--model sonnet` for one-off live submissions on new ATS types until you've validated Haiku on each.
- The benchmark sample is small (3 jobs) and all Greenhouse-Figma. For a more durable conclusion, run 10+ jobs across Robinhood, Chime, Vercel, etc.

