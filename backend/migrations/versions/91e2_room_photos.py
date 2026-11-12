"""Add ordered room photos, preserving existing cover images."""
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

revision = "91e2_room_photos"
down_revision = "73c1_backend_reliability"
branch_labels = None
depends_on = None


def upgrade():
    photos = op.create_table(
        "room_photos",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("room_id", sa.Integer(), sa.ForeignKey("rooms.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("external_url", sa.String(500), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.CheckConstraint("position >= 0 AND position < 5", name="ck_room_photo_position"),
    )
    op.create_index("ix_room_photos_room_id", "room_photos", ["room_id"])
    connection = op.get_bind()
    for row in connection.execute(sa.text("SELECT id, image_url FROM rooms WHERE image_url <> ''")).mappings():
        connection.execute(photos.insert().values(id=uuid4().hex, room_id=row["id"], position=0, external_url=row["image_url"]))


def downgrade():
    # Uploaded assets disappear on downgrade; retain original externally hosted covers.
    op.execute("UPDATE rooms SET image_url = '' WHERE image_url LIKE '/api/room-photos/%'")
    op.drop_index("ix_room_photos_room_id", table_name="room_photos")
    op.drop_table("room_photos")
