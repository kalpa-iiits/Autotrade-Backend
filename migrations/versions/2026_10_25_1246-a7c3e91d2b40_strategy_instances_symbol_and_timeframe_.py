"""strategy instances: symbol and timeframe per instance

Revision ID: a7c3e91d2b40
Revises: 14fe2d4fd8bd
Create Date: 2026-10-25 12:46:14.450514

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c3e91d2b40'
down_revision: Union[str, Sequence[str], None] = '14fe2d4fd8bd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Each deployment can now run as several instances, each with its own symbol and timeframe.
    Existing rows become the first instance of their deployment, on 1m (the old bar_seconds=60)."""
    with op.batch_alter_table("strategy_instances") as batch:
        batch.add_column(sa.Column("deployment_key", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("timeframe", sa.String(length=8), nullable=True))
    op.execute("UPDATE strategy_instances SET deployment_key = key, timeframe = '1m'")
    with op.batch_alter_table("strategy_instances") as batch:
        batch.alter_column("deployment_key", existing_type=sa.String(length=64), nullable=False)
        batch.alter_column("timeframe", existing_type=sa.String(length=8), nullable=False)
        batch.drop_column("key")
        batch.create_index("ix_strategy_instances_deployment_key", ["deployment_key"])
        batch.create_unique_constraint(
            "uq_strategy_instance_target", ["deployment_key", "symbol", "timeframe"]
        )


def downgrade() -> None:
    """Back to one row per deployment. Extra instances keep their id in the key to stay unique."""
    with op.batch_alter_table("strategy_instances") as batch:
        batch.add_column(sa.Column("key", sa.String(length=64), nullable=True))
    op.execute(
        "UPDATE strategy_instances SET key = CASE WHEN id = (SELECT MIN(id) FROM strategy_instances s2 "
        "WHERE s2.deployment_key = strategy_instances.deployment_key) THEN deployment_key "
        "ELSE deployment_key || '-' || id END"
    )
    with op.batch_alter_table("strategy_instances") as batch:
        batch.drop_constraint("uq_strategy_instance_target", type_="unique")
        batch.drop_index("ix_strategy_instances_deployment_key")
        batch.drop_column("timeframe")
        batch.drop_column("deployment_key")
        batch.alter_column("key", existing_type=sa.String(length=64), nullable=False)
        batch.create_unique_constraint("uq_strategy_instances_key", ["key"])
