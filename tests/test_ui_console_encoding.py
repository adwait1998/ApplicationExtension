import io
from pathlib import Path

from rich.console import Console

CLI = Path("src/applypilot/cli.py")


def test_ui_banner_line_is_cp1252_safe():
    """The `applypilot ui` banner must render on a cp1252 console (Windows,
    piped stdout). A literal arrow (U+2192) crashes there — assert the source
    line is ASCII-encodable so the regression is caught at the source."""
    src = CLI.read_text(encoding="utf-8")
    line = next(ln for ln in src.splitlines() if "ApplyPilot dashboard" in ln)
    line.encode("cp1252")            # raises UnicodeEncodeError on U+2192
    assert "→" not in line      # no arrow glyph


def test_rich_render_of_banner_encodes_cp1252():
    buf = io.StringIO()
    c = Console(file=buf, force_terminal=False)
    c.print("[bold]ApplyPilot dashboard[/bold] -> http://127.0.0.1:8765  (Ctrl+C to stop)")
    buf.getvalue().encode("cp1252")  # must not raise
