from __future__ import annotations

from typer.testing import CliRunner

from applypilot import database as db
from applypilot.cli import app
from applypilot.discovery.atlas import boards_repo as repo

runner = CliRunner()


def _redirect_db(tmp_path, monkeypatch):
    """Point the CLI's _bootstrap()/get_connection() at a throwaway tmp DB.

    config.DB_PATH is a module constant captured at import time, and CliRunner
    imports applypilot.cli (-> config) at test-module load, so setting
    APPLYPILOT_DIR here would be too late. database.get_connection()/init_db()
    both key off database.DB_PATH, so patching that (and clearing any cached
    thread-local connection) reliably redirects the whole CLI.
    """
    db_path = tmp_path / "applypilot.db"
    db.close_connection(db_path)
    db.close_connection()
    monkeypatch.setattr(db, "DB_PATH", db_path)


def test_atlas_import_cli_seeds_boards(tmp_path, monkeypatch):
    _redirect_db(tmp_path, monkeypatch)
    result = runner.invoke(app, ["atlas", "import"])
    assert result.exit_code == 0, result.stdout
    conn = db.get_connection()
    # 56 curated tokens land (>= to tolerate any user overlay).
    assert len(repo.get_all(conn)) >= 56


def test_atlas_report_cli_runs(tmp_path, monkeypatch):
    _redirect_db(tmp_path, monkeypatch)
    runner.invoke(app, ["atlas", "import"])
    result = runner.invoke(app, ["atlas", "report"])
    assert result.exit_code == 0, result.stdout
    assert "coverage" in result.stdout.lower() or "boards" in result.stdout.lower()
