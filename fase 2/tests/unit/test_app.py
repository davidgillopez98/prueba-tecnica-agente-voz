from unittest.mock import Mock

from fastapi.testclient import TestClient

from src.api.common.logger import get_logger
from src.main import app
from src.settings import Settings, get_settings
from src.api.workflows.voice_agents import router as voice_router


def test_healthcheck_and_version():
    endpoint_logger = Mock()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        deepgram__api_key="test-key",
        llm__model="test-model",
        llm__endpoint="localhost:11434/api",
        app_version="0.1.0",
    )
    app.dependency_overrides[get_logger] = lambda: endpoint_logger
    try:
        with TestClient(app) as client:
            assert app.state.logger is get_logger()
            health = client.get("/healthcheck")
            assert health.status_code == 200
            assert health.json() == client.get("/").json()
            assert health.json()["service"] == "Prototipo de apertura de siniestros"
            assert client.get("/version").json() == {"version": "0.1.0"}
            assert endpoint_logger.debug.call_count == 3
    finally:
        app.dependency_overrides.clear()


def test_websocket_injects_concrete_settings(monkeypatch):
    received = {}
    endpoint_logger = Mock()

    async def fake_run(self, websocket, **dependencies):
        assert self.logger is endpoint_logger
        received.update(dependencies)
        await websocket.accept()
        await websocket.send_text("ready")
        await websocket.close()

    monkeypatch.setattr(voice_router.VoiceAgentWorkflow, "run", fake_run)
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        deepgram__api_key="test-key",
        llm__model="test-model",
        llm__endpoint="http://localhost:11434/api",
    )
    app.dependency_overrides[get_logger] = lambda: endpoint_logger
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as websocket:
                assert websocket.receive_text() == "ready"
        assert received == {
            "deepgram_api_key": "test-key",
            "llm_model": "test-model",
            "ollama_endpoint": "http://localhost:11434/v1",
        }
        assert endpoint_logger.info.call_count == 2
    finally:
        app.dependency_overrides.clear()
