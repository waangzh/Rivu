from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.util import CommandError
from sqlalchemy import create_engine, inspect, text

from app.core import database


def config_for(connection):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    config.attributes["connection"] = connection
    return config


def test_fresh_schema_has_work_and_tickets_without_retired_graph_tables(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'new.db'}")
    monkeypatch.setattr(database, "sync_engine", engine)
    with engine.connect() as connection:
        command.upgrade(config_for(connection), "head")
        connection.commit()
        tables = set(inspect(connection).get_table_names())
        assert {"review_work_items", "review_candidates", "review_requests", "review_create_intents"} <= tables
        assert not {"review_branch_budgets", "review_model_calls", "preview_jobs", "review_previews"} & tables
        assert not {"thread_id", "mode", "report_markdown"} & {
            column["name"] for column in inspect(connection).get_columns("review_tasks")}
        assert database.schema_is_current()
    engine.dispose()


def test_existing_review_database_is_not_destructively_switched(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.connect() as connection:
        config = config_for(connection)
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20261028_0005')"))
        connection.execute(text("CREATE TABLE review_tasks (id TEXT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO review_tasks VALUES ('old')"))
        connection.execute(text("CREATE TABLE github_publication_attempts (id TEXT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO github_publication_attempts VALUES ('uncertain')"))
        connection.commit()
        with pytest.raises(CommandError, match="Can't locate revision"):
            command.upgrade(config, "head")
        assert connection.scalar(text("SELECT COUNT(*) FROM review_tasks WHERE id='old'")) == 1
        assert "github_publication_attempts" in inspect(connection).get_table_names()
    engine.dispose()


def test_current_vnext_database_upgrades_without_rebuilding_review_tables(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'current.db'}")
    with engine.connect() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('20261031_0001')"))
        connection.execute(text("CREATE TABLE review_tasks (id TEXT PRIMARY KEY, task_snapshot JSON)"))
        connection.execute(text("INSERT INTO review_tasks VALUES ('retained', '{\"status\":\"running\"}')"))
        connection.execute(text("CREATE TABLE review_requests (id TEXT PRIMARY KEY, state TEXT)"))
        connection.execute(text("INSERT INTO review_requests VALUES ('ticket', 'unknown')"))
        connection.execute(text("CREATE TABLE publication_attempts (id TEXT PRIMARY KEY, state TEXT)"))
        connection.execute(text("INSERT INTO publication_attempts VALUES ('receipt', 'uncertain')"))
        connection.commit()
        command.upgrade(config_for(connection), "head")
        connection.commit()
        assert connection.scalar(text("SELECT task_snapshot FROM review_tasks WHERE id='retained'")) == '{"status":"running"}'
        assert connection.scalar(text("SELECT state FROM review_requests")) == "unknown"
        assert connection.scalar(text("SELECT state FROM publication_attempts")) == "uncertain"
        assert "input_id" in {column["name"] for column in inspect(connection).get_columns("review_tasks")}
        assert "local_review_inputs" in inspect(connection).get_table_names()
    engine.dispose()


def test_retained_runs_keep_deadlines_and_dependency_evidence(tmp_path):
    import json

    engine = create_engine(f"sqlite:///{tmp_path / 'retained.db'}")
    snapshot = {"status": "cancelled", "hard_deadline": "2026-11-06T00:00:00Z",
        "dependency_pins": {"old-pin": {"version": "1.2.3"}},
        "configuration": {"options": {"limits": {"model_calls": 9}}}}
    with engine.connect() as connection:
        command.upgrade(config_for(connection), "20261103_0002")
        connection.execute(text("INSERT INTO review_tasks (id, repository, pr_url, status, current_phase, model, provider, "
            "task_snapshot, created_at, updated_at, retention_until) VALUES ('retained', 'o/r', '', 'cancelled', 'cancelled', 'fake', 'test', "
            ":snapshot, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"), {"snapshot": json.dumps(snapshot)})
        connection.commit()
        command.upgrade(config_for(connection), "head")
        connection.commit()
        actual = json.loads(connection.scalar(text("SELECT task_snapshot FROM review_tasks WHERE id='retained'")))
        assert actual == {**snapshot, "first_pass_deadline": None, "first_verification_deadline": None}
    engine.dispose()
