from pathlib import Path


SCRIPT = Path("scripts/production/verify_alembic_state.ps1")


def test_verify_alembic_state_uses_dynamic_head_not_magic_revision():
    body = SCRIPT.read_text(encoding="utf-8")
    assert "$expectedHead" not in body
    assert "ebeab1cf4ec9" not in body
    assert "0b8fffb4c614" not in body
    assert "db heads" in body
    assert "db current" in body
    assert "$dbCurrent -ne $appHead" in body


def test_verify_alembic_state_allows_explicit_python_path():
    body = SCRIPT.read_text(encoding="utf-8")
    assert '[string]$Python = $env:SCANSTORY_PYTHON' in body
    assert "& $Python -m flask --app app db heads" in body
    assert "& $Python -m flask --app app db current" in body
