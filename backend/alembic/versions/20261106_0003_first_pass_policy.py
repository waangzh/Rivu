"""Initialize stage deadlines without changing retained limits, tickets or evidence."""

from alembic import op
import sqlalchemy as sa

revision = "20261106_0003"
down_revision = "20261103_0002"
branch_labels = None
depends_on = None


def upgrade():
    tasks = sa.table("review_tasks", sa.column("id", sa.String), sa.column("task_snapshot", sa.JSON))
    connection = op.get_bind()
    for task_id, snapshot in connection.execute(sa.select(tasks.c.id, tasks.c.task_snapshot)).all():
        if "configuration" not in snapshot:
            continue
        snapshot.setdefault("first_pass_deadline", None)
        snapshot.setdefault("first_verification_deadline", None)
        connection.execute(tasks.update().where(tasks.c.id == task_id).values(task_snapshot=snapshot))


def downgrade():
    raise RuntimeError("Retained run policies and request ledgers must be preserved; use a forward migration.")
