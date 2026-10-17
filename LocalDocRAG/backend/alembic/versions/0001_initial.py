from alembic import op
from app.db.database import SCHEMA

revision = "0001"
down_revision = None


def upgrade():
    for statement in SCHEMA.strip().split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup instead")
