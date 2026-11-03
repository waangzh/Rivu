"""Retain independent local Git inputs without changing existing review payloads."""

from alembic import op
import sqlalchemy as sa

revision = "20261103_0002"
down_revision = "20261031_0001"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "local_review_inputs" not in inspector.get_table_names():
        op.create_table("local_review_inputs",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("manifest", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
        op.create_index("ix_local_review_inputs_expires_at", "local_review_inputs", ["expires_at"])
    if "input_id" not in {column["name"] for column in inspector.get_columns("review_tasks")}:
        op.add_column("review_tasks", sa.Column("input_id", sa.String(32), nullable=True))
        op.create_index("ix_review_tasks_input_id", "review_tasks", ["input_id"])


def downgrade():
    raise RuntimeError("Local inputs may be referenced by retained reviews; use a forward migration.")
