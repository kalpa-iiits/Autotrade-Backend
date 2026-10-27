"""remove strategy mode

Strategies no longer have a dry-run "signal" mode (or a global live switch): a running
instance always places real orders. Instances that were running in signal mode are stopped
here, so none starts placing real orders without someone starting it again.

Revision ID: e5c2b9d4f701
Revises: d3f1a7c2e905
Create Date: 2026-10-27 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5c2b9d4f701'
down_revision: Union[str, Sequence[str], None] = 'd3f1a7c2e905'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "UPDATE strategy_instances SET status = 'stopped', "
        "last_error = 'Signal-only mode was removed: once started, it places real orders' "
        "WHERE mode != 'live' AND status IN ('running', 'error')"
    )
    with op.batch_alter_table('strategy_instances', schema=None) as batch_op:
        batch_op.drop_column('mode')
    with op.batch_alter_table('strategy_signals', schema=None) as batch_op:
        batch_op.drop_column('mode')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('strategy_signals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mode', sa.String(length=8), server_default='live', nullable=False))
    with op.batch_alter_table('strategy_instances', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mode', sa.String(length=8), server_default='live', nullable=False))
