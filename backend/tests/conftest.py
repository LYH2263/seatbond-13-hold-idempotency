import os

# app.database builds the engine from DATABASE_URL at import time; point it at
# sqlite before any app module is imported. Individual tests still swap the
# dependency for an in-memory StaticPool session.
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_seatbond.db")
os.environ.setdefault("SEED_ON_EMPTY", "false")
