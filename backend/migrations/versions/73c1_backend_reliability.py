"""Booking idempotency, durable notifications, site settings and token revocation."""
from alembic import op
import sqlalchemy as sa

revision = "73c1_backend_reliability"
down_revision = "8d47293146d4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("admins", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("payment_transactions") as batch:
        batch.add_column(sa.Column("request_key", sa.String(128), nullable=True))
        batch.create_unique_constraint("uq_payment_transactions_request_key", ["request_key"])
    with op.batch_alter_table("bookings") as batch:
        batch.add_column(sa.Column("idempotency_key", sa.String(128), nullable=True))
        batch.add_column(sa.Column("request_hash", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_bookings_idempotency_key", ["idempotency_key"])
        batch.create_index("ix_bookings_inventory", ["room_id", "status", "check_in", "check_out"])
    op.create_table("site_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("phone", sa.String(32), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("address", sa.String(1000), nullable=False),
        sa.Column("cancellation_policy", sa.Text(), nullable=False))
    op.create_table("email_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dedupe_key", sa.String(200), unique=True, nullable=True),
        sa.Column("recipient", sa.String(254), nullable=False),
        sa.Column("subject", sa.String(250), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lease_token", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True))
    op.create_index("ix_email_outbox_due", "email_outbox", ["status", "available_at"])
    op.create_table("webhook_events",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False))


def downgrade():
    with op.batch_alter_table("payment_transactions") as batch:
        batch.drop_constraint("uq_payment_transactions_request_key", type_="unique")
        batch.drop_column("request_key")
    op.drop_table("webhook_events")
    op.drop_index("ix_email_outbox_due", table_name="email_outbox")
    op.drop_table("email_outbox")
    op.drop_table("site_settings")
    with op.batch_alter_table("bookings") as batch:
        batch.drop_index("ix_bookings_inventory")
        batch.drop_constraint("uq_bookings_idempotency_key", type_="unique")
        batch.drop_column("request_hash")
        batch.drop_column("idempotency_key")
    op.drop_column("admins", "token_version")
