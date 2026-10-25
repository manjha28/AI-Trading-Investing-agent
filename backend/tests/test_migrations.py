import ast
from pathlib import Path


def test_migration_revision_ids_fit_alembic_version_column() -> None:
    versions_dir = Path(__file__).resolve().parents[1] / "migrations" / "versions"

    for migration_path in versions_dir.glob("*.py"):
        tree = ast.parse(migration_path.read_text(encoding="utf-8"))
        revision = next(
            (
                node.value.value
                for node in tree.body
                if isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "revision"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ),
            None,
        )
        if revision is not None:
            assert len(revision) <= 32, (
                f"{migration_path.name} revision {revision!r} has "
                f"{len(revision)} characters; maximum is 32"
            )
