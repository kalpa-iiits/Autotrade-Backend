"""broker accounts

Multi-account: every user adds their own broker accounts (credentials encrypted in
broker_accounts), and orders, order events, fills, positions and strategy instances belong to
one account. Users get personal API tokens (api_tokens) instead of one shared token.

Existing data (from when the server traded one broker set in .env) moves to "Imported (...)"
accounts, one per broker name found, owned by the first user (or a disabled "legacy" user if
there are none). Their credentials are not migrated (they were in .env): paste them into the
account. Running strategies come back stopped. broker_tokens is dropped (tokens are per
account now).

Revision ID: 5e2b8c91d3a4
Revises: c41d0a7e5f12
Create Date: 2026-10-25 17:00:00.000000

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5e2b8c91d3a4'
down_revision: Union[str, Sequence[str], None] = 'c41d0a7e5f12'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TRADING = ("orders", "order_events", "fills", "positions")  # tables with a broker column
KNOWN_BROKERS = {"paper", "fyers", "zerodha"}
# table -> (old index, new index, columns of the new one)
INDEXES = {
    "order_events": ("ix_order_events_order", ["order_id", "id"]),
    "fills": ("ix_fills_symbol_time", ["symbol", "filled_at"]),
    "positions": ("ix_positions_lookup", ["symbol", "status"]),
}


def _orders_columns(first: list[sa.Column]) -> list[sa.Column]:
    return first + [
        sa.Column('client_order_id', sa.String(length=32), nullable=True),
        sa.Column('symbol', sa.String(length=64), nullable=False),
        sa.Column('side', sa.String(length=4), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('order_type', sa.String(length=16), nullable=False),
        sa.Column('limit_price', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('filled_quantity', sa.Integer(), nullable=False),
        sa.Column('filled_price', sa.BigInteger(), nullable=True),
        sa.Column('reason', sa.String(length=512), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    ]


ORDER_DATA = ("client_order_id, symbol, side, quantity, order_type, limit_price, status, "
              "filled_quantity, filled_price, reason, created_at, updated_at")


def _rebuild_orders(columns: list[sa.Column], pk: list[str], client_index: list[str], copy: str) -> None:
    """Primary keys can't be altered in place on SQLite: build the new table and copy."""
    op.create_table('orders_new', *columns, sa.PrimaryKeyConstraint(*pk))
    op.execute(f"INSERT INTO orders_new ({copy}, {ORDER_DATA}) SELECT {copy}, {ORDER_DATA} FROM orders")
    with op.batch_alter_table('orders') as batch_op:
        batch_op.drop_index('ix_orders_client_order_id')
        batch_op.drop_index('ix_orders_updated_at')
        batch_op.drop_index('ix_orders_symbol')
    op.drop_table('orders')
    op.rename_table('orders_new', 'orders')
    with op.batch_alter_table('orders') as batch_op:
        batch_op.create_index('ix_orders_client_order_id', client_index, unique=False)
        batch_op.create_index('ix_orders_updated_at', ['updated_at'], unique=False)
        batch_op.create_index('ix_orders_symbol', ['symbol'], unique=False)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _backfill() -> None:
    conn = op.get_bind()
    has_rows = lambda table: conn.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None
    brokers = set()
    for table in TRADING:
        brokers |= {row[0] for row in conn.execute(sa.text(f"SELECT DISTINCT broker FROM {table}"))}
    instances = has_rows("strategy_instances")
    if not brokers and not instances:
        return

    owner = conn.execute(sa.text("SELECT MIN(id) FROM users")).scalar()
    if owner is None:
        conn.execute(sa.text(
            "INSERT INTO users (username, password_hash, is_active, created_at) "
            "VALUES ('legacy', '!', :inactive, :now)"), {"inactive": False, "now": _now()})
        owner = conn.execute(sa.text("SELECT id FROM users WHERE username = 'legacy'")).scalar()

    account_of = {}
    for name in sorted(brokers or {"paper"}):
        conn.execute(sa.text(
            "INSERT INTO broker_accounts (user_id, broker, label, options, status, created_at, updated_at) "
            "VALUES (:user, :broker, :label, '{}', 'active', :now, :now)"),
            {"user": owner, "broker": name if name in KNOWN_BROKERS else "paper",
             "label": f"Imported ({name})"[:64], "now": _now()})
        account_of[name] = conn.execute(sa.text(
            "SELECT id FROM broker_accounts WHERE user_id = :user AND label = :label"),
            {"user": owner, "label": f"Imported ({name})"[:64]}).scalar()
        for table in TRADING:
            conn.execute(sa.text(f"UPDATE {table} SET account_id = :acct WHERE broker = :broker"),
                         {"acct": account_of[name], "broker": name})

    # Instances had no broker column: they ran on whichever broker was configured.
    first = account_of[sorted(account_of)[0]]
    conn.execute(sa.text("UPDATE strategy_instances SET account_id = :acct"), {"acct": first})
    conn.execute(sa.text(
        "UPDATE strategy_instances SET status = 'stopped', "
        "last_error = 'Moved to a broker account during the upgrade; check it and start again' "
        "WHERE status IN ('running', 'error')"))


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('broker_accounts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('broker', sa.String(length=16), nullable=False),
    sa.Column('label', sa.String(length=64), nullable=False),
    sa.Column('credentials', sa.Text(), nullable=True),
    sa.Column('options', sa.JSON(), nullable=False),
    sa.Column('access_token', sa.Text(), nullable=True),
    sa.Column('token_date', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=8), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_broker_accounts_user_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'label', name='uq_broker_accounts_user_label')
    )
    with op.batch_alter_table('broker_accounts') as batch_op:
        batch_op.create_index('ix_broker_accounts_user_id', ['user_id'], unique=False)

    op.create_table('api_tokens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('prefix', sa.String(length=12), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_api_tokens_user_id'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash', name='uq_api_tokens_token_hash')
    )
    with op.batch_alter_table('api_tokens') as batch_op:
        batch_op.create_index('ix_api_tokens_user_id', ['user_id'], unique=False)

    for table in (*TRADING, "strategy_instances"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column('account_id', sa.Integer(), nullable=True))
    _backfill()

    _rebuild_orders(
        _orders_columns([
            sa.Column('account_id', sa.Integer(), sa.ForeignKey('broker_accounts.id', name='fk_orders_account_id'),
                      nullable=False),
            sa.Column('id', sa.String(length=64), nullable=False),
            sa.Column('broker', sa.String(length=32), nullable=False),
        ]),
        pk=['account_id', 'id'], client_index=['account_id', 'client_order_id'], copy="account_id, id, broker",
    )
    for table, (index, columns) in INDEXES.items():
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column('account_id', existing_type=sa.Integer(), nullable=False)
            batch_op.create_foreign_key(f'fk_{table}_account_id', 'broker_accounts', ['account_id'], ['id'])
            batch_op.drop_index(index)
            batch_op.create_index(index, ['account_id', *columns], unique=False)
    with op.batch_alter_table('strategy_instances') as batch_op:
        batch_op.alter_column('account_id', existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key('fk_strategy_instances_account_id', 'broker_accounts', ['account_id'], ['id'])
        batch_op.drop_constraint('uq_strategy_instance_target', type_='unique')
        batch_op.create_unique_constraint(
            'uq_strategy_instance_target', ['account_id', 'deployment_key', 'symbol', 'timeframe'])
        batch_op.create_index('ix_strategy_instances_account_id', ['account_id'], unique=False)

    op.drop_table('broker_tokens')


def downgrade() -> None:
    """Downgrade schema. Accounts, their credentials and API tokens are deleted; trading rows
    keep their broker name. Fails if two accounts run the same deployment on the same target."""
    op.create_table('broker_tokens',
    sa.Column('broker', sa.String(length=32), nullable=False),
    sa.Column('access_token', sa.String(length=4096), nullable=False),
    sa.Column('token_date', sa.Date(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('broker')
    )
    with op.batch_alter_table('strategy_instances') as batch_op:
        batch_op.drop_index('ix_strategy_instances_account_id')
        batch_op.drop_constraint('uq_strategy_instance_target', type_='unique')
        batch_op.create_unique_constraint('uq_strategy_instance_target', ['deployment_key', 'symbol', 'timeframe'])
        batch_op.drop_constraint('fk_strategy_instances_account_id', type_='foreignkey')
        batch_op.drop_column('account_id')
    for table, (index, columns) in INDEXES.items():
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_index(index)
            batch_op.create_index(index, ['broker', *columns], unique=False)
            batch_op.drop_constraint(f'fk_{table}_account_id', type_='foreignkey')
            batch_op.drop_column('account_id')
    _rebuild_orders(
        _orders_columns([
            sa.Column('broker', sa.String(length=32), nullable=False),
            sa.Column('id', sa.String(length=64), nullable=False),
        ]),
        pk=['broker', 'id'], client_index=['broker', 'client_order_id'], copy="broker, id",
    )
    with op.batch_alter_table('api_tokens') as batch_op:
        batch_op.drop_index('ix_api_tokens_user_id')
    op.drop_table('api_tokens')
    with op.batch_alter_table('broker_accounts') as batch_op:
        batch_op.drop_index('ix_broker_accounts_user_id')
    op.drop_table('broker_accounts')
