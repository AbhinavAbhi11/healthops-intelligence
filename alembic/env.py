from alembic import context
from sqlalchemy import create_engine
from sqlalchemy import pool
from healthops_db.database import database_url
from healthops_db.models import Base

config = context.config
target_metadata = Base.metadata


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name == "healthops"
    return True


def run_migrations_offline():
    url = database_url("migrator")
    context.configure(
        url=url.render_as_string(hide_password=True),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_name=include_name,
        version_table_schema="healthops",
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = create_engine(
        database_url("migrator"),
        poolclass=pool.NullPool,
    )

    try:
        with engine.connect() as connection:
            # The migrator may SET ROLE to the non-login owner role.
            connection.exec_driver_sql("SET ROLE healthops_owner")

            effective_role = connection.exec_driver_sql(
                "SELECT current_user"
            ).scalar_one()

            if effective_role != "healthops_owner":
                raise RuntimeError(
                    "Migration connection did not assume healthops_owner."
                )

            # SET ROLE and the verification query trigger SQLAlchemy 2.x
            # autobegin. Commit that setup transaction before Alembic
            # configures and manages its own migration transaction.
            # PostgreSQL's session-level SET ROLE persists after COMMIT.
            connection.commit()

            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_schemas=True,
                include_name=include_name,
                version_table_schema="healthops",
                compare_type=True,
                compare_server_default=True,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
