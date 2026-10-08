from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_initial_migration_up_and_down(tmp_path):
    db_path = tmp_path / "migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0012_hosted_signin"
    assert {"conversations", "messages", "projects", "tasks", "agent_sessions", "approvals", "memory_entries", "usage_costs", "usage_reservations", "hosted_sessions", "hosted_logins", "alembic_version"} <= set(
        inspect(engine).get_table_names()
    )

    command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]


def test_migration_matches_model_metadata():
    from akaryon.database.models import Base

    assert set(Base.metadata.tables) == {"conversations", "messages", "projects", "tasks", "agent_sessions", "approvals", "memory_entries", "usage_costs", "usage_reservations", "hosted_sessions", "hosted_logins"}


def test_conversation_project_scope_migration_preserves_legacy_rows(tmp_path):
    db_path = tmp_path / "conversation-project-scope.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    command.upgrade(config, "0009_usage_reservations")
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO projects (id, name, path, created_at) VALUES ('project-a', 'A', NULL, CURRENT_TIMESTAMP)"))
        connection.execute(text("INSERT INTO conversations (id, created_at) VALUES ('legacy', CURRENT_TIMESTAMP)"))

    command.upgrade(config, "head")
    with engine.begin() as connection:
        legacy_project = connection.execute(text("SELECT project_id FROM conversations WHERE id = 'legacy'")).scalar_one()
        connection.execute(text("INSERT INTO conversations (id, created_at, project_id) VALUES ('bound', CURRENT_TIMESTAMP, 'project-a')"))
        bound_project = connection.execute(text("SELECT project_id FROM conversations WHERE id = 'bound'")).scalar_one()
    assert legacy_project is None
    assert bound_project == "project-a"
    assert "ix_conversations_project_id" in {index["name"] for index in inspect(engine).get_indexes("conversations")}

    command.downgrade(config, "0009_usage_reservations")
    with engine.begin() as connection:
        ids = set(connection.execute(text("SELECT id FROM conversations")).scalars())
    assert ids == {"legacy", "bound"}
    assert "project_id" not in {column["name"] for column in inspect(engine).get_columns("conversations")}
