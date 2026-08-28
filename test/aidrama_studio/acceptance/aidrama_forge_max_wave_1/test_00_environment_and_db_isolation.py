from __future__ import annotations

import sqlite3
import shutil
from pathlib import Path

import pytest

from aidrama_studio.services.project import ProjectService
from aidrama_studio.storage.database import get_default_paths
from aidrama_studio.storage.repositories import ProjectRepository


def test_canonical_fixture_contract(canonical_project: dict[str, object]) -> None:
    project = canonical_project["project"]
    shots = canonical_project["shots"]
    characters = canonical_project["characters"]
    locations = canonical_project["locations"]
    props = canonical_project["important_props"]

    assert project["target_duration_seconds"] == 60
    assert project["native_generation"] == {
        "width": 1280,
        "height": 720,
        "fps": 24,
        "codec": "h264",
    }
    assert [shot["order"] for shot in shots] == list(range(1, 7))
    assert len({shot["id"] for shot in shots}) == 6
    assert sum(shot["duration_seconds"] for shot in shots) == 60
    assert len(characters) == len(locations) == len(props) == 2
    assert all(shot["dialogue"] for shot in shots)
    assert shots[2]["fake_vision_observation"]["character_lin"] == {
        "wardrobe": "white_shirt",
        "prop": None,
    }


def test_explicit_temporary_database_survives_only_cold_reload(
    repository: ProjectRepository,
    cold_reload,
    database_paths,
    hard_isolation: dict[str, Path],
) -> None:
    project = ProjectService(repository).create(title="Wave 1 isolated database")
    reloaded = cold_reload()

    assert reloaded.get_project(project.id) == project
    assert database_paths.database.is_relative_to(database_paths.root)
    assert not database_paths.database.is_relative_to(
        hard_isolation["blocked_localappdata"]
    )
    assert not (hard_isolation["blocked_localappdata"] / "AIDramaStudio").exists()


def test_default_localappdata_database_attempt_fails_before_open(
    monkeypatch: pytest.MonkeyPatch,
    hard_isolation: dict[str, Path],
) -> None:
    monkeypatch.delenv("AIDRAMA_DATA_DIR")
    default_paths = get_default_paths()
    assert default_paths.root == (
        hard_isolation["blocked_localappdata"] / "AIDramaStudio"
    ).resolve()

    with pytest.raises(AssertionError, match="DEFAULT_LOCALAPPDATA_DB_OPENED=1"):
        ProjectRepository(default_paths)

    assert not default_paths.database.exists()
    # The repository creates its directory before SQLite is reached.  Remove
    # only this deliberately-provoked temporary sentinel so the outer runner
    # can still prove that no unobserved product path escaped isolation.
    shutil.rmtree(default_paths.root)


def test_sqlite_memory_and_fake_credentials_are_allowed_but_never_live(
    fake_credential_store,
    provider_calls,
    canonical_project: dict[str, object],
) -> None:
    with sqlite3.connect(":memory:") as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1

    api_canary = canonical_project["security_canaries"]["api_key"]
    assert fake_credential_store.configured("DASHSCOPE_API_KEY")
    assert fake_credential_store.get("DASHSCOPE_API_KEY") == f"fake-{api_canary}"
    assert provider_calls.real_provider_calls == 0
    assert provider_calls.paid == 0


def test_canonical_fixture_has_required_creative_shape(
    canonical_fixture: dict[str, object],
) -> None:
    project = canonical_fixture["project"]
    assert project["target_duration_seconds"] == 60
    assert project["aspect_ratio"] == "16:9"
    assert len(canonical_fixture["characters"]) == 2
    assert len(canonical_fixture["locations"]) == 2
    assert len(canonical_fixture["important_props"]) == 2

    shots = canonical_fixture["shots"]
    assert len(shots) == 6
    assert [shot["order"] for shot in shots] == list(range(1, 7))
    assert sum(shot["duration_seconds"] for shot in shots) == 60
    assert all(shot["dialogue"] for shot in shots)
    assert shots[2]["fake_vision_observation"]["character_lin"] == {
        "wardrobe": "white_shirt",
        "prop": None,
    }


def test_repository_uses_explicit_temporary_database(
    paths, repository, tmp_path: Path
) -> None:
    project = ProjectService(repository).create(
        title="Wave 1 isolation", description="offline acceptance"
    )

    assert project.id
    assert paths.database.is_file()
    assert str(paths.database).startswith(str(tmp_path))
    assert not (
        tmp_path / "default-localappdata-must-remain-empty" / "AIDramaStudio" / "aidrama.db"
    ).exists()


def test_fake_transport_boundary_starts_with_zero_calls(
    canonical_fixture: dict[str, object],
) -> None:
    scripts = canonical_fixture["provider_scripts"]
    assert scripts["video"]["create_calls_per_shot"] == 1
    assert scripts["vision"]["transport"] == "in_process"
    assert scripts["tts"]["sample_rate"] == 48000
    assert scripts["tts"]["channels"] == 2


def test_default_database_resolution_is_redirected_to_explicit_temp_data_dir(
    offline_environment,
) -> None:
    paths = get_default_paths()
    assert paths == offline_environment.paths
    repository = ProjectRepository()
    created = ProjectService(repository).create(title="Wave 1 temporary DB canary")

    assert repository.paths == offline_environment.paths
    assert repository.get_project(created.id) == created
    assert repository.paths.database.is_file()
    assert not offline_environment.default_db_touched
