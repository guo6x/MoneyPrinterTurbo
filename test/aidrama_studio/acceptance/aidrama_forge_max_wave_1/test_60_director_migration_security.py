from __future__ import annotations

import json
import sqlite3

import pytest

from aidrama_studio.services.security import sanitize_error
from aidrama_studio.storage.database import (
    DatabasePaths,
    connect,
    initialize_database,
)
from aidrama_studio.storage.migrations import (
    MIGRATIONS,
    UnsupportedDatabaseSchemaError,
    apply_migrations,
)
from aidrama_studio.storage.repositories import ProjectRepository
from test.aidrama_studio.test_vision_universal_runtime import (
    FakeVisionSession,
    _vision_context,
    _wired_service,
)


SECURITY_CANARIES = {
    "api_key": "sk-wave1-canary-secret-7f4ac3d2",
    "bearer": "Bearer wave1-bearer-canary-9c41e8",
    "authorization": "Authorization: Bearer wave1-bearer-canary-9c41e8",
    "signed_url": (
        "https://vision-provider.invalid/artifact.mp4?"
        "X-Amz-Signature=wave1-signed-canary-ef93"
    ),
    "private_path": r"C:\\Users\\wave1-private\\secrets\\artifact.mp4",
}


def assert_no_security_canaries(value: object) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    rendered = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    leaked = [needle for needle in SECURITY_CANARIES.values() if needle in rendered]
    assert not leaked, f"public or persisted canary leak: {leaked!r}"


EXPECTED_WAVE_1_SCHEMA_PAYLOADS = {
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
    "auto": {"auto_orchestrator_runs", "auto_agent_events", "auto_paid_authorizations"},
    "audiovisual": {
        "post_dialogue_plans",
        "post_voice_assignment_sets",
        "post_tts_tasks",
        "post_audio_timelines",
    },
}


def _paths(root) -> DatabasePaths:
    return DatabasePaths(root / "aidrama.db", root / "projects", root / "archived")


def _physical_database_at(paths: DatabasePaths, version: int) -> None:
    """Create a synthetic physical historical database without any real DB."""

    paths.database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for migration_version, migration in MIGRATIONS:
            if migration_version > version:
                break
            with connection:
                migration(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'synthetic')",
                    (migration_version,),
                )


def _versions(paths: DatabasePaths) -> list[int]:
    with sqlite3.connect(paths.database) as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]


def _table_names(paths: DatabasePaths) -> set[str]:
    with sqlite3.connect(paths.database) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }


def test_canonical_schema_is_exactly_one_through_thirty_seven_with_all_payloads(
    offline_environment,
) -> None:
    versions = [version for version, _migration in MIGRATIONS]
    assert versions == list(range(1, 38))
    assert len(versions) == len(set(versions)) == 37

    paths = _paths(offline_environment.data_root / "fresh-0-to-37")
    initialize_database(paths)
    assert _versions(paths) == versions
    assert _table_names(paths) >= set().union(*EXPECTED_WAVE_1_SCHEMA_PAYLOADS.values())
    with connect(paths.database) as connection:
        assert apply_migrations(connection) == 0
    # Cold reload verifies the persisted, explicit temporary DB is usable.
    assert ProjectRepository(paths).paths == paths


@pytest.mark.parametrize("historical_version", [31, 32, 33])
def test_synthetic_historical_databases_upgrade_from_each_required_lineage(
    offline_environment,
    historical_version: int,
) -> None:
    paths = _paths(offline_environment.data_root / f"historical-{historical_version}")
    _physical_database_at(paths, historical_version)
    assert _versions(paths) == list(range(1, historical_version + 1))

    initialize_database(paths)
    assert _versions(paths) == list(range(1, 38))
    assert _table_names(paths) >= set().union(*EXPECTED_WAVE_1_SCHEMA_PAYLOADS.values())
    with connect(paths.database) as connection:
        assert apply_migrations(connection) == 0


def test_future_schema_version_fails_closed_before_forward_mutation(
    offline_environment,
) -> None:
    paths = _paths(offline_environment.data_root / "future-schema")
    _physical_database_at(paths, 33)
    with connect(paths.database) as connection:
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (38, 'future')"
        )
        with pytest.raises(UnsupportedDatabaseSchemaError, match="高于当前应用支持"):
            apply_migrations(connection)
    assert _versions(paths) == [*range(1, 34), 38]


class _CanaryFailingVisionSession(FakeVisionSession):
    def request(self, method: str, url: str, **kwargs: object):
        self.calls.append({"method": method, "url": url, **kwargs})
        raise RuntimeError(
            "api_key={api_key}; {authorization}; signed_url={signed_url}; "
            "local={private_path}".format(**SECURITY_CANARIES)
        )


def test_canary_secrets_signed_urls_and_private_paths_do_not_escape_persistence_or_projection(
    offline_environment,
    environment_db_rows,
) -> None:
    repository, project, execution, artifact, *_rest = _vision_context(
        offline_environment.data_root / "security"
    )
    session = _CanaryFailingVisionSession()
    service, _provider, _store, _profile, _resolved = _wired_service(
        repository, project, session
    )

    result = service.analyze(project.id, execution.id, artifact.id)
    assert result.status == "FAILED"
    assert len(session.calls) == 1
    record = repository.get_vision_analysis(result.analysis_id)
    assert record is not None
    durable = [
        result,
        record,
        repository.list_ai_invocations(project.id, execution.id),
        environment_db_rows(),
    ]
    assert_no_security_canaries(durable)
    assert_no_security_canaries(sanitize_error(
        "api_key={api_key}; {authorization}; signed_url={signed_url}; local={private_path}".format(
            **SECURITY_CANARIES
        )
    ))


def test_migration_collision_and_future_schema_fail_closed(tmp_path) -> None:
    """Keep the authoritative FAST_GATE migration node without weakening it."""

    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_61_migration_security_regression as migration_regression,
    )

    migration_regression.test_migration_collision_and_future_schema_fail_closed(
        tmp_path
    )


def test_security_canaries_are_absent_from_public_and_persistent_surfaces(
    canonical_approved_project: dict[str, object],
    canonical_project: dict[str, object],
    fake_credential_store,
    assert_public_safe,
    hard_isolation,
) -> None:
    """Keep the authoritative FAST_GATE security node backed by the cold scan."""

    from test.aidrama_studio.acceptance.aidrama_forge_max_wave_1 import (
        test_61_migration_security_regression as security_regression,
    )

    security_regression.test_security_canaries_are_absent_from_public_and_persistent_surfaces(
        canonical_approved_project,
        canonical_project,
        fake_credential_store,
        assert_public_safe,
        hard_isolation,
    )
