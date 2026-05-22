"""ApplyPilot autonomous apply reliability loop.

This package implements the helpers consumed by the fixed driver prompt
that ralph-loop re-fires each iteration. See
`docs/superpowers/specs/2026-05-22-autonomous-apply-reliability-loop-design.md`
for the full design.

CARVEOUT POLICY (enforced by safety.is_immutable_path):
  Modules in this package are LOOP-IMMUTABLE. The loop must never edit
  its own grader (state, signature_extractor, safety, runner) or the
  driver prompt. The grader cannot grade itself.
"""
