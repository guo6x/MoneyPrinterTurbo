from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import requests

from aidrama_studio.domain import (
    Character,
    Location,
    Scene,
    ScriptBeat,
    ScriptBeatType,
    ScriptRevisionStatus,
    Shot,
    ShotPlan,
    ShotRevisionStatus,
    StoryBeat,
    StoryBible,
    StoryRevisionStatus,
    StructuredScript,
    World,
)
from aidrama_studio.services.project import ProjectService
from aidrama_studio.storage.database import DatabasePaths
from aidrama_studio.storage.repositories import ProjectRepository


ACCEPTANCE_ROOT = Path(__file__).resolve().parent


@dataclass
class FakeCredentialStore:
    """Explicit in-memory credential seam; values never leave this object."""

    values: dict[str, str] = field(default_factory=dict)

    def configured(self, key: str) -> bool:
        return bool(self.values.get(key))

    def configured_providers(self) -> tuple[str, ...]:
        return tuple(sorted(key for key, value in self.values.items() if value))

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


@dataclass
class ProviderCallLedger:
    llm: int = 0
    image: int = 0
    video_create: int = 0
    video_poll: int = 0
    vision: int = 0
    tts: int = 0
    paid: int = 0
    real_provider_calls: int = 0


@dataclass(frozen=True)
class OfflineEnvironment:
    """Compatibility projection shared by the absorbed offline test shards."""

    data_root: Path
    blocked_localappdata: Path

    @property
    def paths(self) -> DatabasePaths:
        return DatabasePaths(
            database=self.data_root / "aidrama.db",
            projects=self.data_root / "projects",
            archived_projects=self.data_root / "archived_projects",
        )

    @property
    def default_db_touched(self) -> bool:
        return (self.blocked_localappdata / "AIDramaStudio").exists()


@pytest.fixture(scope="session")
def canonical_project() -> dict[str, Any]:
    return json.loads((ACCEPTANCE_ROOT / "canonical_project.json").read_text(encoding="utf-8"))


@pytest.fixture
def provider_calls() -> ProviderCallLedger:
    return ProviderCallLedger()


@pytest.fixture
def fake_credential_store(canonical_project: dict[str, Any]) -> FakeCredentialStore:
    canary = canonical_project["security_canaries"]["api_key"]
    return FakeCredentialStore(
        {
            "DASHSCOPE_API_KEY": f"fake-{canary}",
            "DEEPSEEK_API_KEY": f"fake-{canary}",
            "ARK_API_KEY": f"fake-{canary}",
        }
    )


@pytest.fixture(autouse=True)
def hard_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_calls: ProviderCallLedger,
) -> Iterator[dict[str, Path]]:
    """Fail before network or default LocalAppData can become test evidence."""

    explicit_root = (tmp_path / "explicit-aidrama-data").resolve()
    blocked_localappdata = (tmp_path / "default-localappdata-must-remain-empty").resolve()
    blocked_aidrama = blocked_localappdata / "AIDramaStudio"
    monkeypatch.setenv("AIDRAMA_DATA_DIR", str(explicit_root))
    monkeypatch.setenv("AIDRAMA_SQLITE_WAL", "0")
    monkeypatch.setenv("AIDRAMA_TEST_NO_NETWORK", "1")
    monkeypatch.setenv("LOCALAPPDATA", str(blocked_localappdata))
    monkeypatch.delenv("AIDRAMA_ALLOW_PAID", raising=False)
    monkeypatch.delenv("AIDRAMA_ALLOW_PAID_LIVE_TESTS", raising=False)

    original_connect = sqlite3.connect

    def guarded_sqlite_connect(database: object, *args: object, **kwargs: object):
        if str(database) != ":memory:":
            candidate = Path(str(database)).expanduser().resolve()
            try:
                candidate.relative_to(blocked_localappdata)
            except ValueError:
                pass
            else:
                raise AssertionError(
                    f"DEFAULT_LOCALAPPDATA_DB_OPENED=1 path={candidate}"
                )
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_sqlite_connect)

    def deny_network(*_args: object, **_kwargs: object) -> None:
        provider_calls.real_provider_calls += 1
        raise AssertionError("REAL_PROVIDER_OR_NETWORK_CALL_FORBIDDEN")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    monkeypatch.setattr(requests.sessions.Session, "request", deny_network)
    monkeypatch.setattr(urllib.request, "urlopen", deny_network)

    yield {
        "explicit_root": explicit_root,
        "blocked_localappdata": blocked_localappdata,
    }

    assert not blocked_aidrama.exists(), (
        f"DEFAULT_LOCALAPPDATA_DB_OPENED=1 path={blocked_aidrama}"
    )


@pytest.fixture
def database_paths(tmp_path: Path) -> DatabasePaths:
    root = tmp_path / "explicit-database"
    return DatabasePaths(
        database=root / "aidrama.db",
        projects=root / "projects",
        archived_projects=root / "archived",
    )


@pytest.fixture
def paths(database_paths: DatabasePaths) -> DatabasePaths:
    return database_paths


@pytest.fixture(scope="session")
def canonical_fixture(canonical_project: dict[str, Any]) -> dict[str, Any]:
    return canonical_project


@pytest.fixture
def cold_repository(paths: DatabasePaths) -> ProjectRepository:
    return ProjectRepository(paths)


@pytest.fixture
def offline_environment(hard_isolation: dict[str, Path]) -> OfflineEnvironment:
    return OfflineEnvironment(
        data_root=hard_isolation["explicit_root"],
        blocked_localappdata=hard_isolation["blocked_localappdata"],
    )


@pytest.fixture
def temporary_paths(offline_environment: OfflineEnvironment) -> DatabasePaths:
    return offline_environment.paths


@pytest.fixture
def repository(database_paths: DatabasePaths) -> ProjectRepository:
    return ProjectRepository(database_paths)


@pytest.fixture
def canonical_approved_project(
    repository: ProjectRepository,
    canonical_project: dict[str, Any],
) -> dict[str, object]:
    """Persist the canonical six-shot approved creative chain."""

    fixture_project = canonical_project["project"]
    fixture_characters = canonical_project["characters"]
    fixture_locations = canonical_project["locations"]
    fixture_shots = canonical_project["shots"]
    project = ProjectService(repository).create(
        title=str(fixture_project["title"]),
        description=str(fixture_project["logline"]),
        target_duration_seconds=int(fixture_project["target_duration_seconds"]),
    )
    story = StoryBible(
        title=str(fixture_project["title"]),
        logline=str(fixture_project["logline"]),
        premise=str(fixture_project["logline"]),
        genre="Drama",
        tone="Rainy and restrained",
        world=World(era="Contemporary", setting="Old bookshop on a rainy night"),
        characters=[
            Character(
                id=str(item["id"]),
                name=str(item["name"]),
                role="lead",
                appearance=(
                    f"wardrobe={item['baseline']['wardrobe']}; "
                    f"prop={item['baseline']['prop']}"
                ),
            )
            for item in fixture_characters
        ],
        locations=[
            Location(
                id=str(item["id"]),
                name=str(item["name"]),
                environment=str(item["name"]),
                time_of_day="NIGHT",
            )
            for item in fixture_locations
        ],
        story_beats=[
            StoryBeat(
                id="story_beat_01",
                order=1,
                type="OPENING",
                summary="The red umbrella is returned",
                characters=["character_lin", "character_su"],
                location_id="location_bookshop_exterior",
            ),
            StoryBeat(
                id="story_beat_02",
                order=2,
                type="DEVELOPMENT",
                summary="They read the delayed letter",
                characters=["character_lin", "character_su"],
                location_id="location_bookshop_interior",
            ),
            StoryBeat(
                id="story_beat_03",
                order=3,
                type="ENDING",
                summary="They choose to continue together",
                characters=["character_lin", "character_su"],
                location_id="location_bookshop_exterior",
            ),
        ],
    )
    script_beats = [
        ScriptBeat(
            id=f"script_beat_{index:02d}",
            order=index,
            type=ScriptBeatType.DIALOGUE,
            character_id=("character_lin" if index % 2 else "character_su"),
            text=str(item["dialogue"]),
            estimated_duration_seconds=int(item["duration_seconds"]),
        )
        for index, item in enumerate(fixture_shots, start=1)
    ]
    script = StructuredScript(
        title=str(fixture_project["title"]),
        scenes=[
            Scene(
                id="scene_exterior",
                order=1,
                title="Bookshop exterior",
                location_id="location_bookshop_exterior",
                character_ids=["character_lin", "character_su"],
                estimated_duration_seconds=40,
                beats=[script_beats[index] for index in (0, 1, 4, 5)],
                source_story_beat_ids=["story_beat_01", "story_beat_03"],
            ),
            Scene(
                id="scene_interior",
                order=2,
                title="Bookshop interior",
                location_id="location_bookshop_interior",
                character_ids=["character_lin", "character_su"],
                estimated_duration_seconds=20,
                beats=[script_beats[index] for index in (2, 3)],
                source_story_beat_ids=["story_beat_02"],
            ),
        ],
    )
    shot_plan = ShotPlan(
        title=f"{fixture_project['title']} shot plan",
        source_script_revision_id="script_001",
        shots=[
            Shot(
                id=str(item["id"]),
                order=int(item["order"]),
                scene_id=(
                    "scene_interior"
                    if item["location"] == "location_bookshop_interior"
                    else "scene_exterior"
                ),
                source_script_beat_ids=[f"script_beat_{index:02d}"],
                duration_seconds=int(item["duration_seconds"]),
                subject=list(item["characters"]),
                action=str(item["dialogue"]),
                visual_intent=f"Canonical shot {index}",
            )
            for index, item in enumerate(fixture_shots, start=1)
        ],
    )
    now = "2026-08-28T00:00:00+00:00"
    repository.create_story_revision(
        revision_id="story_001",
        project_id=project.id,
        version=1,
        status=StoryRevisionStatus.APPROVED,
        content=story,
        generation_input={"fixture": canonical_project["fixture_id"]},
        created_at=now,
        updated_at=now,
    )
    repository.create_script_revision(
        revision_id="script_001",
        project_id=project.id,
        version=1,
        status=ScriptRevisionStatus.APPROVED,
        source_story_revision_id="story_001",
        content=script,
        generation_input={"source_story_revision_id": "story_001"},
        created_at=now,
        updated_at=now,
    )
    repository.create_shot_revision(
        revision_id="shot_plan_001",
        project_id=project.id,
        version=1,
        status=ShotRevisionStatus.APPROVED,
        source_script_revision_id="script_001",
        content=shot_plan,
        generation_input={"source_script_revision_id": "script_001"},
        created_at=now,
        updated_at=now,
    )
    return {
        "repository": repository,
        "project": project,
        "story": story,
        "script": script,
        "shot_plan": shot_plan,
    }


@pytest.fixture
def cold_reload(database_paths: DatabasePaths) -> Callable[[], ProjectRepository]:
    return lambda: ProjectRepository(database_paths)


@pytest.fixture(scope="session")
def ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


@pytest.fixture
def ffmpeg_executable(ffmpeg_path: str) -> str:
    return ffmpeg_path


@pytest.fixture
def environment_db_rows(offline_environment: OfflineEnvironment):
    """Return a cold scan of every persisted value in the explicit test DB."""

    def read_rows() -> list[object]:
        database = offline_environment.paths.database
        if not database.exists():
            return []
        with sqlite3.connect(database) as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            values: list[object] = []
            for (name,) in tables:
                if name.startswith("sqlite_"):
                    continue
                quoted = '"' + name.replace('"', '""') + '"'
                values.extend(
                    tuple(row)
                    for row in connection.execute(f"SELECT * FROM {quoted}")
                )
            return values

    return read_rows


@pytest.fixture
def assert_public_safe(canonical_project: dict[str, Any]) -> Callable[[object], None]:
    canaries = canonical_project["security_canaries"]
    forbidden = tuple(str(value) for value in canaries.values())

    def check(value: object) -> None:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        for canary in forbidden:
            assert canary not in serialized

    return check
