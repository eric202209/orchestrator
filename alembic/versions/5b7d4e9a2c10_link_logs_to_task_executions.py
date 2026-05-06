"""link_logs_to_task_executions

Revision ID: 5b7d4e9a2c10
Revises: 9f2a7c6d1e3b
Create Date: 2026-05-05 00:00:01.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "5b7d4e9a2c10"
down_revision: Union[str, Sequence[str], None] = "9f2a7c6d1e3b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("log_entries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("task_execution_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_log_entries_task_execution_id_task_executions",
            "task_executions",
            ["task_execution_id"],
            ["id"],
        )
        batch_op.create_index(
            batch_op.f("ix_log_entries_task_execution_id"),
            ["task_execution_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("log_entries", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_log_entries_task_execution_id"))
        batch_op.drop_constraint(
            "fk_log_entries_task_execution_id_task_executions",
            type_="foreignkey",
        )
        batch_op.drop_column("task_execution_id")
