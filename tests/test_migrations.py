import os
import subprocess
import sys


def test_migrations_upgrade_and_downgrade(tmp_path):
    env = {**os.environ, "ENVIRONMENT": "test", "DATABASE_URL": f"sqlite:///{tmp_path / 'migration.db'}"}
    for target in ("head", "8d47293146d4", "head"):
        direction = "downgrade" if target == "8d47293146d4" else "upgrade"
        result = subprocess.run([sys.executable, "-m", "alembic", direction, target], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    result = subprocess.run([sys.executable, "-m", "alembic", "check"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
