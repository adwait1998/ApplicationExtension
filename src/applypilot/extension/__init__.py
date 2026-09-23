"""ApplyPilot Copilot — local resolution service for the Chrome extension.

Human-in-the-loop counterpart to the autonomous pipeline in
``applypilot.apply``: the operator opens a job application in their own
browser, the extension reads the form, and this package decides which
profile value belongs in each field. The extension fills and highlights;
the human reviews and clicks submit. Nothing here ever submits anything.

See docs/superpowers/specs/2026-09-23-copilot-extension-design.md.
"""
