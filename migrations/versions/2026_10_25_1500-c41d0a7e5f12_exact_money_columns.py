"""exact money columns

Prices and P&L were floats. They become BIGINT counts of 1/10000ths (app.core.db.Money), so
sums of P&L are exact. Existing values are scaled first, then the column type changes.

Revision ID: c41d0a7e5f12
Revises: 900261617dfe
Create Date: 2026-10-25 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c41d0a7e5f12'
down_revision: Union[str, Sequence[str], None] = '900261617dfe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCALE = 10_000
# table -> (column, nullable)
COLUMNS = {
    "orders": [("limit_price", True), ("filled_price", True)],
    "order_events": [("filled_price", True)],
    "fills": [("price", False)],
    "positions": [("avg_entry_price", False), ("avg_exit_price", True), ("realized_pnl", False)],
}


def upgrade() -> None:
    """Upgrade schema."""
    for table, columns in COLUMNS.items():
        for column, _ in columns:
            op.execute(f"UPDATE {table} SET {column} = ROUND({column} * {SCALE}) WHERE {column} IS NOT NULL")
        with op.batch_alter_table(table, schema=None) as batch_op:
            for column, nullable in columns:
                batch_op.alter_column(column, existing_type=sa.Float(), type_=sa.BigInteger(),
                                      existing_nullable=nullable)


def downgrade() -> None:
    """Downgrade schema."""
    for table, columns in COLUMNS.items():
        with op.batch_alter_table(table, schema=None) as batch_op:
            for column, nullable in columns:
                batch_op.alter_column(column, existing_type=sa.BigInteger(), type_=sa.Float(),
                                      existing_nullable=nullable)
        for column, _ in columns:
            op.execute(f"UPDATE {table} SET {column} = {column} * 1.0 / {SCALE} WHERE {column} IS NOT NULL")
