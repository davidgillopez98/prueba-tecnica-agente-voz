import logging
import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta

from pipecat.flows.config import FlowConfig
from pipecat.flows.flow import Flow

from src.api.workflows.voice_agents.ws_workflow.claims import ClaimsService
from src.api.workflows.voice_agents.ws_workflow.mocks.services import MockInsuranceServices
from src.api.workflows.voice_agents.ws_workflow.mocks.repository import ClaimRepository
from src.api.workflows.voice_agents.ws_workflow.tools import FlowTools
from src.api.workflows.voice_agents.ws_workflow.workflow import FLOW_FILE


def service(tmp_path):
    return ClaimsService(MockInsuranceServices(), ClaimRepository(tmp_path / "claims.sqlite3"), logging.getLogger("test_claims"))


def incident(**overrides):
    return {
        "incident_date": (date.today() - timedelta(days=1)).isoformat(),
        "location": "Madrid centro",
        "incident_type": "colision",
        "description": "Choque leve en cruce",
        "damages": "Paragolpes delantero",
    } | overrides


def identify(claims, state, dni="123456", otp="111111"):
    assert claims.retrieve_user_data(state, dni)["status"] == "ok"
    assert claims.generate_otp(state)["status"] == "ok"
    assert claims.verify_otp(state, otp)["route"] == "comprobacion"


def cover(claims, state, data=None):
    assert claims.categorize_incident(state, data or incident())["status"] == "ok"
    assert claims.retrieve_policy(state)["route"] == "resumen"


def test_separate_tools_and_private_user_data(tmp_path):
    claims = service(tmp_path)
    state = {}
    result = claims.retrieve_user_data(state, "123456")
    assert result["status"] == "ok"
    assert "phone" not in str(result) and "USR-100" not in str(result)
    assert state["user_data"]["phone"] == "+34000000100"
    assert state["user_data"]["user_id"] == "USR-100"
    assert claims.verify_otp(state, "111111")["status"] == "invalid_step"
    assert claims.generate_otp(state)["status"] == "ok"
    assert claims.verify_otp(state, "111111")["route"] == "comprobacion"


def test_corrected_dni_invalidates_old_otp(tmp_path):
    claims = service(tmp_path)
    state = {}
    claims.retrieve_user_data(state, "SYN-200")
    claims.generate_otp(state)
    claims.retrieve_user_data(state, "123456")
    assert claims.verify_otp(state, "222222")["status"] == "invalid_step"
    claims.generate_otp(state)
    assert claims.verify_otp(state, "222222")["status"] == "identity_failed"
    assert state["otp_attempts"] == 1
    assert state["dni_attempts"] == 0
    assert state["otp_pending"] is True
    assert claims.verify_otp(state, "111111")["route"] == "comprobacion"
    assert state["otp_attempts"] == 0


def test_dni_and_otp_have_separate_user_attempt_limits(tmp_path):
    claims = service(tmp_path)
    state = {}
    for expected in (1, 2):
        result = claims.retrieve_user_data(state, "SYN-999")
        assert result["route"] == "identificacion"
        assert "repitan" in result["message"]
        assert state["dni_attempts"] == expected
        assert "otp_attempts" not in state
    claims.retrieve_user_data(state, "123456")
    assert state["dni_attempts"] == 0
    claims.generate_otp(state)
    for expected in (1, 2):
        result = claims.verify_otp(state, "bad")
        assert result["route"] == "identificacion"
        assert "repitan" in result["message"]
        assert state["otp_attempts"] == expected
        assert state["dni_attempts"] == 0
        assert state["otp_pending"] is True
    result = claims.verify_otp(state, "bad")
    assert result["route"] == "derivacion"
    assert state["otp_attempts"] == 3
    assert "user_data" not in state


def test_three_wrong_dnis_handoff_without_otp_attempts(tmp_path):
    claims = service(tmp_path)
    state = {}
    for _ in range(3):
        result = claims.retrieve_user_data(state, "SYN-999")
    assert result["route"] == "derivacion"
    assert state["dni_attempts"] == 3
    assert "otp_attempts" not in state


def test_categorization_and_coverage_are_separate(tmp_path):
    claims = service(tmp_path)
    state = {}
    identify(claims, state)
    assert claims.retrieve_policy(state)["status"] == "invalid_step"
    assert claims.categorize_incident(state, incident())["category"] == "colision"
    assert "stage" not in state
    result = claims.retrieve_policy(state)
    assert result["route"] == "resumen"
    assert state["policy"]["policy_id"] == "POL-100"
    assert "POL-100" not in str(result)


def test_all_categories_ambiguity_and_uncovered(tmp_path):
    claims = service(tmp_path)
    state = {}
    identify(claims, state)
    cases = {
        "robo": "Robo del vehículo aparcado",
        "incendio": "Incendio en el motor",
        "danos_por_agua": "Inundación por agua en el garaje",
        "rotura_de_lunas": "Rotura de parabrisas delantero",
    }
    for category, description in cases.items():
        assert claims.categorize_incident(state, incident(incident_type=category, description=description))["category"] == category
    assert claims.categorize_incident(state, incident(description="Choque y robo"))["status"] == "ambiguous_incident"
    assert claims.categorize_incident(state, incident(incident_type="robo"))["status"] == "contradictory_incident"
    assert claims.categorize_incident(state, incident(incident_type="incendio", description="Incendio en el motor"))["status"] == "ok"
    assert claims.retrieve_policy(state)["status"] == "uncovered"
    assert "stage" not in state


def test_inactive_policy_and_missing_fields(tmp_path):
    claims = service(tmp_path)
    state = {}
    identify(claims, state, "SYN-200", "222222")
    assert claims.categorize_incident(state, {"description": "Choque"})["status"] == "invalid_data"
    assert claims.categorize_incident(state, incident())["status"] == "ok"
    assert claims.retrieve_policy(state)["status"] == "uncovered"


def test_correction_overwrites_incident_and_revalidates_coverage(tmp_path):
    claims = service(tmp_path)
    state = {}
    identify(claims, state)
    cover(claims, state)
    assert state["coverage_approved"] is True
    # FlowManager ya ha vuelto a comprobación; la siguiente clasificación
    # sustituye el incidente y borra la cobertura anterior.
    assert claims.categorize_incident(state, incident(incident_type="incendio", description="Incendio en el motor"))["status"] == "ok"
    assert state["incident_category"] == "incendio"
    assert "policy" not in state and "coverage_approved" not in state
    assert claims.open_claim(state)["status"] == "invalid_step"
    assert claims.retrieve_policy(state)["status"] == "uncovered"
    assert claims.open_claim(state)["status"] == "invalid_step"


def test_claim_needs_coverage_and_deduplicates(tmp_path):
    claims = service(tmp_path)
    state = {}
    identify(claims, state)
    assert claims.open_claim(state)["status"] == "invalid_step"
    cover(claims, state)
    first_result = claims.open_claim(state)
    first = first_result["claim"]
    assert first_result["message"] == "Parte registrado correctamente."
    retry_result = claims.open_claim(state)
    assert retry_result["claim"] == first
    assert retry_result["message"] == "Parte ya registrado."
    assert state["closing_message"].startswith("El parte ya estaba registrado.")
    assert claims.repository.get(first["claim_id"])["incident"]["description"] == "Choque leve en cruce"
    second = {}
    identify(claims, second)
    cover(claims, second, incident(
        description="Accidente leve en un cruce",
        damages="Abolladura del parachoques",
    ))
    duplicate_result = claims.open_claim(second)
    assert duplicate_result["claim"] == first
    assert duplicate_result["message"] == "Parte ya registrado."
    assert second["closing_message"].startswith("El parte ya estaba registrado.")
    assert claims.repository.get_by_request_id(second["request_id"]) == first
    assert claims.repository.get(first["claim_id"])["incident"]["description"] == "Choque leve en cruce"

    different_location = {}
    identify(claims, different_location)
    cover(claims, different_location, incident(location="Madrid norte"))
    assert claims.open_claim(different_location)["claim"]["claim_id"] != first["claim_id"]
    later = (date.today() + timedelta(days=1)).isoformat() + "T00:00:00+00:00"
    duplicate = claims.repository.create(
        second["request_id"], "CLM-SHOULD-NOT-EXIST", later,
        claims.repository.get(first["claim_id"])["incident"], "unused",
    )
    assert duplicate == first


def test_new_request_after_dedup_window_can_create_claim(tmp_path, monkeypatch):
    claims = service(tmp_path)
    first_state = {}
    identify(claims, first_state)
    cover(claims, first_state)
    first = claims.open_claim(first_state)["claim"]
    later = datetime.fromisoformat(first["created_at"]) + timedelta(minutes=11)

    class LaterDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return later.astimezone(tz) if tz else later.replace(tzinfo=None)

    monkeypatch.setattr(
        "src.api.workflows.voice_agents.ws_workflow.claims.datetime", LaterDatetime
    )
    second_state = {}
    identify(claims, second_state)
    cover(claims, second_state)
    result = claims.open_claim(second_state)

    assert result["status"] == "ok"
    assert result["message"] == "Parte registrado correctamente."
    assert result["claim"]["claim_id"] != first["claim_id"]
    assert datetime.fromisoformat(result["claim"]["created_at"]) == later
    assert claims.repository.get(first["claim_id"]) is not None
    assert claims.repository.get(result["claim"]["claim_id"]) is not None
    # Una solicitud antigua conserva su idempotencia incluso fuera de la ventana.
    first_state.pop("claim")
    retry = claims.open_claim(first_state)
    assert retry["claim"] == first
    assert retry["message"] == "Parte ya registrado."


def test_existing_fingerprint_is_rebuilt_for_free_text_deduplication(tmp_path):
    claims = service(tmp_path)
    original_state = {}
    identify(claims, original_state)
    cover(claims, original_state)
    original = claims.open_claim(original_state)["claim"]
    database = tmp_path / "claims.sqlite3"
    original_payload = claims.repository.get(original["claim_id"])["incident"]
    old_fingerprint = hashlib.sha256(
        json.dumps(original_payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE claim_fingerprints SET fingerprint = ?", (old_fingerprint,))

    reopened = service(tmp_path)
    retry_state = {}
    identify(reopened, retry_state)
    cover(reopened, retry_state, incident(
        description="Accidente leve en un cruce",
        damages="Abolladura del parachoques",
    ))
    assert reopened.open_claim(retry_state)["claim"]["claim_id"] == original["claim_id"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM claims").fetchone()[0] == 1


def test_three_exception_attempts_then_failure(tmp_path, monkeypatch):
    claims = service(tmp_path)
    calls = 0

    def outage(_dni):
        nonlocal calls
        calls += 1
        raise TimeoutError("simulated outage")

    monkeypatch.setattr(claims.insurer, "get_user_data", outage)
    state = {}
    assert claims.retrieve_user_data(state, "123456")["route"] == "derivacion"
    assert calls == 3
    assert "dni_attempts" not in state


def test_three_write_exceptions_never_announce_claim(tmp_path, monkeypatch):
    claims = service(tmp_path)
    state = {}
    identify(claims, state)
    cover(claims, state)
    original = claims.repository.create
    calls = 0

    def outage(*args):
        nonlocal calls
        calls += 1
        raise OSError("simulated outage")

    monkeypatch.setattr(claims.repository, "create", outage)
    result = claims.open_claim(state)
    assert calls == 3
    assert result["status"] == "dependency_error"
    assert result["route"] == "apertura" and "claim" not in state
    monkeypatch.setattr(claims.repository, "create", original)
    assert claims.open_claim(state)["route"] == "cierre"


def test_flow_exposes_business_tools_and_navigation_in_every_stage(tmp_path):
    config = FlowConfig.from_file(str(FLOW_FILE))
    claims = service(tmp_path)
    tools = FlowTools(claims, logging.getLogger("test_flow"))
    handlers = {name: getattr(tools, name) for name in (
        "recuperar_datos_usuario", "generar_otp", "validar_otp",
        "categorizar_incidente", "recuperar_poliza", "crear_parte",
    )}
    flow = Flow(config, handlers=handlers)
    names = {function.name for node in config.nodes.values() for function in node.functions}
    assert names == {
        "recuperar_datos_usuario", "generar_otp", "validar_otp",
        "categorizar_incidente", "recuperar_poliza", "crear_parte",
        "confirmar_resumen", "volver_a_comprobacion", "derivar_a_humano",
    }
    for stage in ("identificacion", "comprobacion", "resumen", "apertura", "cierre"):
        human = next(function for function in config.nodes[stage].functions if function.name == "derivar_a_humano")
        assert human.transition_only and human.transition_to == "derivacion"
    confirmation = next(function for function in config.nodes["resumen"].functions if function.name == "confirmar_resumen")
    assert confirmation.transition_only and confirmation.transition_to == "apertura"
    correction = next(function for function in config.nodes["resumen"].functions if function.name == "volver_a_comprobacion")
    assert correction.transition_only and correction.transition_to == "comprobacion"
    assert config.nodes["resumen"].respond_immediately is False
    assert flow.node("apertura")["name"] == "apertura"
