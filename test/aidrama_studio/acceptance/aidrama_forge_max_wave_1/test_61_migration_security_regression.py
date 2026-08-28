from __future__ import annotations

import sqlite3

import pytest

from aidrama_studio.services import AutoOrchestratorService
from aidrama_studio.services.director import DirectorService
from aidrama_studio.services.model_settings import SettingsModelService
from aidrama_studio.storage.migrations import (
    MIGRATIONS,
    UnsupportedDatabaseSchemaError,
    apply_migrations,
)
from test.aidrama_studio.test_migrations import (
    test_canonical_forward_upgrade_from_historical_schema_fixtures as _historical_upgrade_contract,
)


PAYLOAD_TABLES = {
    "continuity": {
        "continuity_snapshots",
        "continuity_issues",
        "continuity_repair_recommendations",
    },
    "reliability": {
        "paid_budget_ledgers",
        "paid_create_reservations",
        "production_artifact_identities",
    },
    "creative": {"creative_pipeline_operations"},
    "auto": {
        "auto_orchestrator_runs",
        "auto_agent_events",
        "auto_paid_authorizations",
        "auto_paid_consumptions",
    },
    "audiovisual_schema_only": {
        "post_dialogue_plans",
        "post_voice_assignment_sets",
        "post_tts_tasks",
        "post_audio_timelines",
    },
}


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def test_migration_collision_and_future_schema_fail_closed(tmp_path) -> None:
    versions = [version for version, _migration in MIGRATIONS]
    assert versions == list(range(1, 38))
    assert len(versions) == len(set(versions))

    database = tmp_path / "fresh-0-to-37.db"
    with sqlite3.connect(database) as connection:
        assert apply_migrations(connection) == 37
        assert apply_migrations(connection) == 0
        assert [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ] == versions
        actual_tables = _tables(connection)
        for payload, required in PAYLOAD_TABLES.items():
            assert required <= actual_tables, payload

        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (38, 'future')"
        )
        connection.commit()
        before_schema = tuple(
            connection.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall()
        )
        before_versions = tuple(
            connection.execute(
                "SELECT version,applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
        )
        with pytest.raises(UnsupportedDatabaseSchemaError, match="38.*37"):
            apply_migrations(connection)
        after_schema = tuple(
            connection.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall()
        )
        after_versions = tuple(
            connection.execute(
                "SELECT version,applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
        )
        assert after_schema == before_schema
        assert after_versions == before_versions


@pytest.mark.parametrize("recorded_through", [31, 32, 33])
def test_historical_schema_fixture_upgrades_to_37_without_row_rewrite(
    recorded_through: int,
) -> None:
    _historical_upgrade_contract(recorded_through)


def test_existing_director_projection_uses_injected_temp_repository_only(
    canonical_approved_project: dict[str, object],
    hard_isolation,
) -> None:
    repository = canonical_approved_project["repository"]
    project = canonical_approved_project["project"]
    director = DirectorService(repository)

    session = director.start_session(project.id, max_steps=1)
    projection = director.inspect_project(project.id)
    reconstructed = DirectorService(repository).reconstruct(project.id, session.id)

    assert director.repository is repository
    assert projection["project_id"] == project.id
    assert reconstructed["session"].id == session.id
    assert not (
        hard_isolation["blocked_localappdata"] / "AIDramaStudio"
    ).exists()
def test_security_canaries_are_absent_from_public_and_persistent_surfaces(
    canonical_approved_project: dict[str, object],
    canonical_project: dict[str, object],
    fake_credential_store,
    assert_public_safe,
    hard_isolation,
) -> None:
    repository = canonical_approved_project["repository"]
    project = canonical_approved_project["project"]
    settings = SettingsModelService(
        repository,
        credential_store=fake_credential_store,
    )
    director = DirectorService(repository)
    session = director.start_session(project.id, max_steps=1)
    public_surfaces = {
        "settings_inventory": settings.inventory(),
        "credential_requirements": settings.credential_requirements(),
        "auto_decision": AutoOrchestratorService(repository).next_action(project.id),
        "director_project": director.inspect_project(project.id),
        "director_reconstruct": director.reconstruct(project.id, session.id),
    }

    assert_public_safe(public_surfaces)
    database_bytes = repository.paths.database.read_bytes()
    for canary in canonical_project["security_canaries"].values():
        assert str(canary).encode("utf-8") not in database_bytes
        assert str(canary).encode("utf-16-le") not in database_bytes
    assert not (
        hard_isolation["blocked_localappdata"] / "AIDramaStudio"
    ).exists()
