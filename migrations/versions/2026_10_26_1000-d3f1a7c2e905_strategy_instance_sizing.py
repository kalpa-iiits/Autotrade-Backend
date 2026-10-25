"""strategy instance sizing

Each instance's position sizing, chosen in the app: a fixed quantity or an amount of capital
per trade.

Revision ID: d3f1a7c2e905
Revises: b7d2e4f6a813
Create Date: 2026-10-26 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3f1a7c2e905'
down_revision: Union[str, Sequence[str], None] = 'b7d2e4f6a813'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('strategy_instances', schema=None) as batch_op:
        batch_op.add_column(sa.Column('size_mode', sa.String(length=8), server_default='fixed', nullable=False))
        batch_op.add_column(sa.Column('fixed_quantity', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('capital_per_trade', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('strategy_instances', schema=None) as batch_op:
        batch_op.drop_column('capital_per_trade')
        batch_op.drop_column('fixed_quantity')
        batch_op.drop_column('size_mode')
