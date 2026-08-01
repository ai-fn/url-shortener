"""add users and link ownership

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-30 19:23:26.928212
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )

    # Nullable first: existing M2-era rows have no owner. Fail loudly below rather
    # than silently deleting them on the operator's behalf.
    op.add_column("links", sa.Column("owner_id", sa.UUID(), nullable=True))

    bind = op.get_bind()
    orphaned = bind.execute(sa.text("SELECT count(*) FROM links WHERE owner_id IS NULL")).scalar()
    if orphaned:
        raise RuntimeError(
            f"{orphaned} existing link(s) have no owner; links.owner_id cannot be made "
            "NOT NULL. Assign an owner_id to them or, if this is disposable local/test "
            "data, run `TRUNCATE TABLE links;` yourself, then re-run `alembic upgrade "
            "head`. This migration will not delete rows for you."
        )

    op.alter_column("links", "owner_id", nullable=False)

    op.create_foreign_key(
        op.f("fk_links_owner_id_users"), "links", "users", ["owner_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_index(op.f("ix_links_owner_id"), "links", ["owner_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_links_owner_id"), table_name="links")
    op.drop_constraint(op.f("fk_links_owner_id_users"), "links", type_="foreignkey")
    op.drop_column("links", "owner_id")
    op.drop_table("users")
