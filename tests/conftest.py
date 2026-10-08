from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from cusdev.config import Settings, get_settings
from cusdev.db import get_session, make_engine
from cusdev.models import Base
from cusdev.taxonomy import Taxonomy, get_taxonomy
from cusdev.web.app import app

ROOT = Path(__file__).parent.parent
AGENT = {"Authorization": "Bearer agent-secret"}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        taxonomy_path=ROOT / "config" / "taxonomy.yaml",
        agent_token="agent-secret",
        access_key="",
    )


@pytest.fixture
def taxonomy(settings) -> Taxonomy:
    return Taxonomy.load(settings.taxonomy_path)


@pytest.fixture
def session_factory(settings) -> sessionmaker[Session]:
    settings.data_dir.mkdir(parents=True)
    engine = make_engine(f"sqlite:///{settings.database_path}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory) -> Iterator[Session]:
    with session_factory() as s:
        yield s


@pytest.fixture
def client(settings, taxonomy, session_factory) -> Iterator[TestClient]:
    def _session() -> Iterator[Session]:
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_taxonomy] = lambda: taxonomy
    app.dependency_overrides[get_session] = _session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
