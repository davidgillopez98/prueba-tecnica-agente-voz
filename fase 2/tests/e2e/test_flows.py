"""El WebSocket, pipeline, Flow y FlowManager reales ejecutan las seis tools."""

import sqlite3
from datetime import date, timedelta

from fastapi.testclient import TestClient
from pipecat.flows.manager import FlowManager
from pipecat.pipeline.worker import PipelineWorker

from src.main import app
from src.settings import Settings, get_settings
from src.api.workflows.voice_agents.ws_workflow.mocks.repository import ClaimRepository
from src.api.workflows.voice_agents.ws_workflow.workflow import GREETING
from tests.e2e.service_mocks import LLMAction, MockVoiceServices, VoiceScenario


def incident(**overrides):
    return {
        "incident_date": (date.today() - timedelta(days=1)).isoformat(),
        "location": "Madrid centro",
        "incident_type": "colision",
        "description": "Choque leve en cruce",
        "damages": "Paragolpes delantero",
    } | overrides


def summary():
    data = incident()
    return (
        f"Fecha: {data['incident_date']}; ubicación: {data['location']}; "
        f"tipo: colision; descripción: {data['description']}; "
        f"daños: {data['damages']}."
    )


def configure(tmp_path):
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, deepgram__api_key="fake-deepgram", llm__model="fake-model",
        llm__endpoint="localhost:11434/api", claims_db_path=tmp_path / "claims.sqlite3",
    )


def receive_voice(websocket):
    return websocket.receive_bytes().rstrip(b"\x00")


def heard(websocket, spoken, audio):
    assert receive_voice(websocket) == ("VOICE:" + spoken).encode()
    websocket.send_bytes(audio)


def test_claim_happy_path_with_real_pipecat(monkeypatch, tmp_path):
    scenario = VoiceScenario(
        transcripts={b"identity": "123456", b"otp": "111111", b"incident": "Choque en Madrid", b"confirm": "Sí, confirmo"},
        llm_actions=(
            LLMAction(text=GREETING),
            LLMAction(tool="recuperar_datos_usuario", arguments={"document_id": "123456"}),
            LLMAction(tool="generar_otp"),
            LLMAction(text="Indique el código de verificación."),
            LLMAction(tool="validar_otp", arguments={"verification_value": "111111"}),
            LLMAction(text="Describa el incidente."),
            LLMAction(tool="categorizar_incidente", arguments=incident()),
            LLMAction(tool="recuperar_poliza"),
            LLMAction(tool="confirmar_resumen"),
            LLMAction(tool="crear_parte"),
        ),
    )
    mocks = MockVoiceServices(monkeypatch, tmp_path, scenario)
    configure(tmp_path)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as websocket:
                heard(websocket, GREETING, b"identity")
                heard(websocket, "Indique el código de verificación.", b"otp")
                heard(websocket, "Describa el incidente.", b"incident")
                heard(websocket, f"Le resumo el incidente: {summary()} ¿Lo confirma o desea corregirlo?", b"confirm")
                closing = receive_voice(websocket).decode()
    finally:
        app.dependency_overrides.clear()
    assert isinstance(mocks.manager, FlowManager)
    assert isinstance(mocks.manager.worker, PipelineWorker)
    assert mocks.manager.current_node == "cierre"
    assert closing == (
        f"VOICE:El parte ha quedado registrado. Su identificador es {mocks.manager.state['claim']['claim_id']}. "
        "Si quiere, anótelo para consultar su estado en la web."
    )
    assert mocks.tool_calls == [
        "recuperar_datos_usuario", "generar_otp", "validar_otp",
        "categorizar_incidente", "recuperar_poliza", "crear_parte",
    ]
    assert mocks.manager.state["claim"]["status"] == "abierto"
    assert mocks.navigation_calls == ["confirmar_resumen"]
    assert GREETING in mocks.context_snapshots[0]
    assert "USR-100" not in " ".join(mocks.spoken)
    assert "+34000000100" not in " ".join(mocks.spoken)
    assert all("USR-100" not in context and "+34000000100" not in context and "POL-100" not in context for context in mocks.context_snapshots)


def test_retrying_same_claim_in_new_session_creates_one_record(monkeypatch, tmp_path):
    configure(tmp_path)
    claim_ids = []
    closings = []
    try:
        for description, damages in (
            ("Choque leve en cruce", "Paragolpes delantero"),
            ("Accidente leve en un cruce", "Abolladura del parachoques"),
        ):
            incident_data = incident(location="Sevilla", description=description, damages=damages)
            scenario = VoiceScenario(
                transcripts={
                    b"dni": "123456", b"otp": "111111",
                    b"incident": description, b"confirm": "Sí, confirmo",
                },
                llm_actions=(
                    LLMAction(text=GREETING),
                    LLMAction(tool="recuperar_datos_usuario", arguments={"document_id": "123456"}),
                    LLMAction(tool="generar_otp"),
                    LLMAction(text="Indique el código."),
                    LLMAction(tool="validar_otp", arguments={"verification_value": "111111"}),
                    LLMAction(text="Describa el incidente."),
                    LLMAction(tool="categorizar_incidente", arguments=incident_data),
                    LLMAction(tool="recuperar_poliza"),
                    LLMAction(tool="confirmar_resumen"),
                    LLMAction(tool="crear_parte"),
                ),
            )
            with monkeypatch.context() as patches:
                mocks = MockVoiceServices(patches, tmp_path, scenario)
                with TestClient(app) as client:
                    with client.websocket_connect("/ws") as websocket:
                        heard(websocket, GREETING, b"dni")
                        heard(websocket, "Indique el código.", b"otp")
                        heard(websocket, "Describa el incidente.", b"incident")
                        expected_summary = (
                            summary().replace("Madrid centro", "Sevilla")
                            .replace("Choque leve en cruce", description)
                            .replace("Paragolpes delantero", damages)
                        )
                        heard(websocket, f"Le resumo el incidente: {expected_summary} ¿Lo confirma o desea corregirlo?", b"confirm")
                        closing = receive_voice(websocket).decode()
                assert mocks.manager.current_node == "cierre"
                assert mocks.tool_calls.count("crear_parte") == 1
                claim_id = mocks.manager.state["claim"]["claim_id"]
                assert claim_id in closing
                claim_ids.append(claim_id)
                closings.append(closing)
    finally:
        app.dependency_overrides.clear()

    assert claim_ids[0] == claim_ids[1]
    assert closings[0].startswith("VOICE:El parte ha quedado registrado.")
    assert closings[1].startswith("VOICE:El parte ya estaba registrado.")
    with sqlite3.connect(tmp_path / "claims.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
    stored = ClaimRepository(tmp_path / "claims.sqlite3").get(claim_ids[0])
    assert stored["incident"]["description"] == "Choque leve en cruce"


def test_ambiguous_confirmation_stays_in_summary_without_claim(monkeypatch, tmp_path):
    scenario = VoiceScenario(
        transcripts={b"identity": "123456", b"otp": "111111", b"incident": "Choque en Madrid", b"unclear": "Puede ser"},
        llm_actions=(
            LLMAction(text=GREETING),
            LLMAction(tool="recuperar_datos_usuario", arguments={"document_id": "123456"}),
            LLMAction(tool="generar_otp"),
            LLMAction(text="Indique el código."),
            LLMAction(tool="validar_otp", arguments={"verification_value": "111111"}),
            LLMAction(text="Describa el incidente."),
            LLMAction(tool="categorizar_incidente", arguments=incident()),
            LLMAction(tool="recuperar_poliza"),
            LLMAction(text="Necesito que confirme el resumen de forma clara."),
        ),
    )
    mocks = MockVoiceServices(monkeypatch, tmp_path, scenario)
    configure(tmp_path)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as websocket:
                heard(websocket, GREETING, b"identity")
                heard(websocket, "Indique el código.", b"otp")
                heard(websocket, "Describa el incidente.", b"incident")
                heard(websocket, f"Le resumo el incidente: {summary()} ¿Lo confirma o desea corregirlo?", b"unclear")
                assert receive_voice(websocket) == "VOICE:Necesito que confirme el resumen de forma clara.".encode()
    finally:
        app.dependency_overrides.clear()
    assert mocks.manager.current_node == "resumen"
    assert "claim" not in mocks.manager.state
    assert "crear_parte" not in mocks.tool_calls
    assert mocks.navigation_calls == []


def test_invalid_identity_hands_off_without_policy_lookup(monkeypatch, tmp_path):
    scenario = VoiceScenario(
        transcripts={b"bad1": "SYN-999", b"bad2": "SYN-999", b"bad3": "SYN-999"},
        llm_actions=(
            LLMAction(text=GREETING),
            LLMAction(tool="recuperar_datos_usuario", arguments={"document_id": "SYN-999"}),
            LLMAction(text="Repita el DNI."),
            LLMAction(tool="recuperar_datos_usuario", arguments={"document_id": "SYN-999"}),
            LLMAction(text="Repita el DNI por última vez."),
            LLMAction(tool="recuperar_datos_usuario", arguments={"document_id": "SYN-999"}),
            LLMAction(text="Se requiere atención humana."),
        ),
    )
    mocks = MockVoiceServices(monkeypatch, tmp_path, scenario)
    configure(tmp_path)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as websocket:
                heard(websocket, GREETING, b"bad1")
                heard(websocket, "Repita el DNI.", b"bad2")
                heard(websocket, "Repita el DNI por última vez.", b"bad3")
                assert receive_voice(websocket) == "VOICE:Se requiere atención humana.".encode()
    finally:
        app.dependency_overrides.clear()
    assert mocks.manager.current_node == "derivacion"
    assert mocks.manager.state["dni_attempts"] == 3
    assert mocks.tool_calls == ["recuperar_datos_usuario"] * 3
    assert "claim" not in mocks.manager.state


def test_uncovered_incident_stays_in_checking_without_claim(monkeypatch, tmp_path):
    scenario = VoiceScenario(
        transcripts={b"dni": "SYN-300", b"otp": "333333", b"incident": "Choque en Madrid"},
        llm_actions=(
            LLMAction(text=GREETING),
            LLMAction(tool="recuperar_datos_usuario", arguments={"document_id": "SYN-300"}),
            LLMAction(tool="generar_otp"),
            LLMAction(text="Indique el código."),
            LLMAction(tool="validar_otp", arguments={"verification_value": "333333"}),
            LLMAction(text="Describa el incidente."),
            LLMAction(tool="categorizar_incidente", arguments=incident()),
            LLMAction(tool="recuperar_poliza"),
            LLMAction(text="La póliza no cubre esa clase de incidente."),
        ),
    )
    mocks = MockVoiceServices(monkeypatch, tmp_path, scenario)
    configure(tmp_path)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as websocket:
                heard(websocket, GREETING, b"dni")
                heard(websocket, "Indique el código.", b"otp")
                heard(websocket, "Describa el incidente.", b"incident")
                assert receive_voice(websocket) == "VOICE:La póliza no cubre esa clase de incidente.".encode()
    finally:
        app.dependency_overrides.clear()
    assert mocks.manager.current_node == "comprobacion"
    assert mocks.manager.state["coverage_approved"] is False
    assert "claim" not in mocks.manager.state
    assert "crear_parte" not in mocks.tool_calls


def test_repository_failure_never_enters_closing(monkeypatch, tmp_path):
    calls = 0

    def unavailable(self, *args):
        nonlocal calls
        calls += 1
        raise OSError("simulated repository outage")

    scenario = VoiceScenario(
        transcripts={b"dni": "123456", b"otp": "111111", b"incident": "Choque en Madrid", b"confirm": "Sí, confirmo"},
        llm_actions=(
            LLMAction(text=GREETING),
            LLMAction(tool="recuperar_datos_usuario", arguments={"document_id": "123456"}),
            LLMAction(tool="generar_otp"),
            LLMAction(text="Indique el código."),
            LLMAction(tool="validar_otp", arguments={"verification_value": "111111"}),
            LLMAction(text="Describa el incidente."),
            LLMAction(tool="categorizar_incidente", arguments=incident()),
            LLMAction(tool="recuperar_poliza"),
            LLMAction(tool="confirmar_resumen"),
            LLMAction(tool="crear_parte"),
            LLMAction(text="No he podido confirmar el alta; puede reintentar o pedir atención humana."),
        ),
    )
    mocks = MockVoiceServices(monkeypatch, tmp_path, scenario)
    monkeypatch.setattr(ClaimRepository, "create", unavailable)
    configure(tmp_path)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as websocket:
                heard(websocket, GREETING, b"dni")
                heard(websocket, "Indique el código.", b"otp")
                heard(websocket, "Describa el incidente.", b"incident")
                heard(websocket, f"Le resumo el incidente: {summary()} ¿Lo confirma o desea corregirlo?", b"confirm")
                assert receive_voice(websocket) == "VOICE:No he podido confirmar el alta; puede reintentar o pedir atención humana.".encode()
    finally:
        app.dependency_overrides.clear()
    assert calls == 3
    assert mocks.manager.current_node == "apertura"
    assert "claim" not in mocks.manager.state
    assert "cierre" not in mocks.navigation_calls


def test_corrected_dni_replaces_otp_in_real_flow(monkeypatch, tmp_path):
    scenario = VoiceScenario(
        transcripts={b"first": "SYN-200", b"correction": "El DNI correcto es 123456", b"otp": "111111"},
        llm_actions=(
            LLMAction(text=GREETING),
            LLMAction(tool="recuperar_datos_usuario", arguments={"document_id": "SYN-200"}),
            LLMAction(tool="generar_otp"),
            LLMAction(text="Indique el código o corrija el DNI."),
            LLMAction(tool="recuperar_datos_usuario", arguments={"document_id": "123456"}),
            LLMAction(tool="generar_otp"),
            LLMAction(text="Indique el nuevo código."),
            LLMAction(tool="validar_otp", arguments={"verification_value": "111111"}),
            LLMAction(text="Describa el incidente."),
        ),
    )
    mocks = MockVoiceServices(monkeypatch, tmp_path, scenario)
    configure(tmp_path)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as websocket:
                heard(websocket, GREETING, b"first")
                heard(websocket, "Indique el código o corrija el DNI.", b"correction")
                heard(websocket, "Indique el nuevo código.", b"otp")
                assert receive_voice(websocket) == b"VOICE:Describa el incidente."
    finally:
        app.dependency_overrides.clear()
    assert mocks.manager.current_node == "comprobacion"
    assert mocks.manager.state["user_data"]["user_id"] == "USR-100"
    assert mocks.tool_calls == [
        "recuperar_datos_usuario", "generar_otp", "recuperar_datos_usuario",
        "generar_otp", "validar_otp",
    ]


def test_misheard_otp_can_be_repeated_without_handoff(monkeypatch, tmp_path):
    scenario = VoiceScenario(
        transcripts={b"dni": "123456", b"bad": "111112", b"correct": "111111"},
        llm_actions=(
            LLMAction(text=GREETING),
            LLMAction(tool="recuperar_datos_usuario", arguments={"document_id": "123456"}),
            LLMAction(tool="generar_otp"),
            LLMAction(text="Indique el código."),
            LLMAction(tool="validar_otp", arguments={"verification_value": "111112"}),
            LLMAction(text="No se ha podido verificar. Repita el código."),
            LLMAction(tool="validar_otp", arguments={"verification_value": "111111"}),
            LLMAction(text="Describa el incidente."),
        ),
    )
    mocks = MockVoiceServices(monkeypatch, tmp_path, scenario)
    configure(tmp_path)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as websocket:
                heard(websocket, GREETING, b"dni")
                heard(websocket, "Indique el código.", b"bad")
                heard(websocket, "No se ha podido verificar. Repita el código.", b"correct")
                assert receive_voice(websocket) == "VOICE:Describa el incidente.".encode()
    finally:
        app.dependency_overrides.clear()
    assert mocks.manager.current_node == "comprobacion"
    assert mocks.manager.state["otp_attempts"] == 0
    assert mocks.tool_calls == [
        "recuperar_datos_usuario", "generar_otp", "validar_otp", "validar_otp",
    ]


def test_transition_only_returns_to_checking_and_revalidates(monkeypatch, tmp_path):
    scenario = VoiceScenario(
        transcripts={b"dni": "123456", b"otp": "111111", b"incident": "Choque en cruce", b"correction": "No, fue un incendio", b"new_incident": "Fue un incendio en el motor"},
        llm_actions=(
            LLMAction(text=GREETING),
            LLMAction(tool="recuperar_datos_usuario", arguments={"document_id": "123456"}),
            LLMAction(tool="generar_otp"),
            LLMAction(text="Indique el código."),
            LLMAction(tool="validar_otp", arguments={"verification_value": "111111"}),
            LLMAction(text="Describa el incidente."),
            LLMAction(tool="categorizar_incidente", arguments=incident()),
            LLMAction(tool="recuperar_poliza"),
            LLMAction(tool="volver_a_comprobacion"),
            LLMAction(text="Volvamos a comprobar el incidente."),
            LLMAction(tool="categorizar_incidente", arguments=incident(incident_type="incendio", description="Incendio en el motor")),
            LLMAction(tool="recuperar_poliza"),
            LLMAction(text="Esa clase no está cubierta; revisemos los datos."),
        ),
    )
    mocks = MockVoiceServices(monkeypatch, tmp_path, scenario)
    configure(tmp_path)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as websocket:
                heard(websocket, GREETING, b"dni")
                heard(websocket, "Indique el código.", b"otp")
                heard(websocket, "Describa el incidente.", b"incident")
                heard(websocket, f"Le resumo el incidente: {summary()} ¿Lo confirma o desea corregirlo?", b"correction")
                heard(websocket, "Volvamos a comprobar el incidente.", b"new_incident")
                assert receive_voice(websocket) == "VOICE:Esa clase no está cubierta; revisemos los datos.".encode()
    finally:
        app.dependency_overrides.clear()
    assert mocks.manager.current_node == "comprobacion"
    assert "stage" not in mocks.manager.state
    assert mocks.manager.state["incident_category"] == "incendio"
    assert mocks.tool_calls == [
        "recuperar_datos_usuario", "generar_otp", "validar_otp",
        "categorizar_incidente", "recuperar_poliza",
        "categorizar_incidente", "recuperar_poliza",
    ]
    assert mocks.manager.state["coverage_approved"] is False
    assert "claim" not in mocks.manager.state
    assert mocks.navigation_calls == ["volver_a_comprobacion"]


def test_corrected_summary_is_the_one_persisted(monkeypatch, tmp_path):
    corrected = incident(location="Madrid norte")
    corrected_summary = summary().replace("Madrid centro", "Madrid norte")
    scenario = VoiceScenario(
        transcripts={
            b"dni": "123456", b"otp": "111111", b"incident": "Choque en cruce",
            b"correction": "No, fue en Madrid norte", b"new_incident": "Choque en Madrid norte",
            b"confirm": "Sí, confirmo",
        },
        llm_actions=(
            LLMAction(text=GREETING),
            LLMAction(tool="recuperar_datos_usuario", arguments={"document_id": "123456"}),
            LLMAction(tool="generar_otp"),
            LLMAction(text="Indique el código."),
            LLMAction(tool="validar_otp", arguments={"verification_value": "111111"}),
            LLMAction(text="Describa el incidente."),
            LLMAction(tool="categorizar_incidente", arguments=incident()),
            LLMAction(tool="recuperar_poliza"),
            LLMAction(tool="volver_a_comprobacion"),
            LLMAction(text="Corrijamos la ubicación."),
            LLMAction(tool="categorizar_incidente", arguments=corrected),
            LLMAction(tool="recuperar_poliza"),
            LLMAction(tool="confirmar_resumen"),
            LLMAction(tool="crear_parte"),
        ),
    )
    mocks = MockVoiceServices(monkeypatch, tmp_path, scenario)
    configure(tmp_path)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as websocket:
                heard(websocket, GREETING, b"dni")
                heard(websocket, "Indique el código.", b"otp")
                heard(websocket, "Describa el incidente.", b"incident")
                heard(websocket, f"Le resumo el incidente: {summary()} ¿Lo confirma o desea corregirlo?", b"correction")
                heard(websocket, "Corrijamos la ubicación.", b"new_incident")
                heard(websocket, f"Le resumo el incidente: {corrected_summary} ¿Lo confirma o desea corregirlo?", b"confirm")
                closing = receive_voice(websocket).decode()
    finally:
        app.dependency_overrides.clear()
    assert mocks.manager.current_node == "cierre"
    claim_id = mocks.manager.state["claim"]["claim_id"]
    assert claim_id in closing
    stored = ClaimRepository(tmp_path / "claims.sqlite3").get(claim_id)
    assert stored["incident"]["location"] == "Madrid norte"
    assert mocks.navigation_calls == ["volver_a_comprobacion", "confirmar_resumen"]
    assert mocks.tool_calls.count("recuperar_poliza") == 2


def test_human_handoff_uses_transition_only(monkeypatch, tmp_path):
    scenario = VoiceScenario(
        transcripts={b"human": "Quiero hablar con una persona"},
        llm_actions=(
            LLMAction(text=GREETING),
            LLMAction(tool="derivar_a_humano"),
            LLMAction(text="Se requiere atención humana."),
        ),
    )
    mocks = MockVoiceServices(monkeypatch, tmp_path, scenario)
    configure(tmp_path)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as websocket:
                heard(websocket, GREETING, b"human")
                assert receive_voice(websocket) == "VOICE:Se requiere atención humana.".encode()
    finally:
        app.dependency_overrides.clear()
    assert mocks.manager.current_node == "derivacion"
    assert "stage" not in mocks.manager.state
    assert mocks.navigation_calls == ["derivar_a_humano"]
    assert mocks.tool_calls == []
