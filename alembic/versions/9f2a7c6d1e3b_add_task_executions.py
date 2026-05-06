"""add_task_executions

Revision ID: 9f2a7c6d1e3b
Revises: db2fbcd464f4
Create Date: 2026-05-05 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9f2a7c6d1e3b"
down_revision: Union[str, Sequence[str], None] = "db2fbcd464f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "task_executions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "RUNNING",
                "FAILED",
                "DONE",
                "CANCELLED",
                name="taskstatus",
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "task_id",
            "attempt_number",
            name="uq_task_executions_session_task_attempt",
        ),
    )
    with op.batch_alter_table("task_executions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_task_executions_id"), ["id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_task_executions_session_id"), ["session_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_task_executions_task_id"), ["task_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("task_executions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_task_executions_task_id"))
        batch_op.drop_index(batch_op.f("ix_task_executions_session_id"))
        batch_op.drop_index(batch_op.f("ix_task_executions_id"))

    op.drop_table("task_executions")
