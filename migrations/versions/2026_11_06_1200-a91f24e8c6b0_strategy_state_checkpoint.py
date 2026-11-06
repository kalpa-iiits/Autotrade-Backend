"""strategy state checkpoint

Revision ID: a91f24e8c6b0
Revises: e5c2b9d4f701
Create Date: 2026-11-06 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a91f24e8c6b0"
down_revision: Union[str, Sequence[str], None] = "e5c2b9d4f701"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("strategy_instances", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("strategy_state", sa.JSON(), server_default="{}", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("strategy_instances", schema=None) as batch_op:
        batch_op.drop_column("strategy_state")
