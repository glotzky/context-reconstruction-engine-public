from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from cre.api import create_app
from cre.config import Settings


def test_health_returns_version_and_commit(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "cre-test",
        "version": "9.9.9-test",
        "commit": "deadbeef",
    }


def test_health_works_without_database() -> None:
    app = create_app(Settings(DATABASE_URL="", CRE_VERSION="9.9.9-test", CRE_COMMIT="deadbeef"))
    assert app.state.engine is None
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_does_not_touch_injected_engine(settings: Settings) -> None:
    engine = MagicMock(name="engine")
    app = create_app(settings, engine=engine)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    engine.connect.assert_not_called()
    engine.begin.assert_not_called()
