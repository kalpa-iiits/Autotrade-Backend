"""orders trigger_price

Stop orders (SL / SL-M) carry a trigger price, stored like the other prices (app.core.db.Money).

Revision ID: b7d2e4f6a813
Revises: 5e2b8c91d3a4
Create Date: 2026-10-26 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d2e4f6a813'
down_revision: Union[str, Sequence[str], None] = '5e2b8c91d3a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('trigger_price', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('orders', schema=None) as batch_op:
        batch_op.drop_column('trigger_price')
