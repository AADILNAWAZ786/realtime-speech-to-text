"""Guards against packaging/registration mistakes that tests run from a working copy would
otherwise miss (e.g. a .gitignore rule silently excluding a source package)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
SOURCE_DIRS = ("app", "alembic", "tests", "scripts")


def _in_git_repo() -> bool:
    if shutil.which("git") is None:
        return False
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],  # noqa: S607
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


@pytest.mark.skipif(not _in_git_repo(), reason="not inside a git working tree")
def test_no_source_file_is_git_ignored() -> None:
    sources = [
        str(path.relative_to(BACKEND_DIR))
        for directory in SOURCE_DIRS
        for path in (BACKEND_DIR / directory).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],  # noqa: S607
        cwd=BACKEND_DIR,
        input="\n".join(sources),
        capture_output=True,
        text=True,
        check=False,
    )
    ignored = result.stdout.split()
    assert ignored == [], f"Source files ignored by .gitignore: {ignored}"


def test_all_models_registered_on_alembic_metadata() -> None:
    """Alembic autogenerate only sees tables registered on Base.metadata via app.models."""
    import app.models as models

    assert set(models.__all__) == {"Base", "User", "Transcription", "TranscriptionSegment"}
    assert set(models.Base.metadata.tables) == {
        "users",
        "transcriptions",
        "transcription_segments",
    }
    for model in (models.User, models.Transcription, models.TranscriptionSegment):
        assert model.metadata is models.Base.metadata


def test_alembic_env_targets_application_metadata() -> None:
    env_source = (BACKEND_DIR / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "from app.models import Base" in env_source
    assert "target_metadata = Base.metadata" in env_source


def test_env_example_lists_only_real_settings() -> None:
    """Every variable in .env.example must map to a settings field (catches typos)."""
    from app.core.config import Settings
    from app.speech.config import SpeechSettings

    keys = [
        line.split("=", 1)[0].strip()
        for line in (BACKEND_DIR / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    ]
    settings_fields = {name.upper() for name in Settings.model_fields}
    whisper_fields = {f"WHISPER_{name.upper()}" for name in SpeechSettings.model_fields}
    unknown = [k for k in keys if k not in settings_fields | whisper_fields | {"TEST_DATABASE_URL"}]
    assert unknown == []
    assert "DATABASE_URL" in keys and "JWT_SECRET_KEY" in keys
