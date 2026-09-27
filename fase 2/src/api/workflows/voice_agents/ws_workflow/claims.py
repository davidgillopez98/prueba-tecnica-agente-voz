"""Estado privado, precondiciones y transiciones del parte sintético."""

import logging
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import ValidationError

from .mocks.repository import ClaimRepository, claim_fingerprint
from .mocks.services import MockInsuranceServices
from .schemas import Incident


class RetryExhausted(Exception):
    """Los tres intentos de una dependencia han lanzado una excepción."""


class ClaimsService:
    MAX_DNI_ATTEMPTS = 3
    MAX_OTP_ATTEMPTS = 3

    def __init__(self, insurer: MockInsuranceServices, repository: ClaimRepository, logger: logging.Logger):
        self.insurer = insurer
        self.repository = repository
        self.logger = logger

    def retrieve_user_data(self, state: dict, document_id: str) -> dict:
        # Cada DNI nuevo sustituye por completo el reto anterior.
        for key in ("user_data", "otp_pending", "identity_verified", "otp_attempts"):
            state.pop(key, None)
        try:
            user_data = self._retry(self.insurer.get_user_data, document_id)
        except RetryExhausted:
            return self._dependency_failure(state, "No se pudieron consultar los datos de usuario.")
        if user_data is None:
            return self._identity_input_failed(state, "dni_attempts", self.MAX_DNI_ATTEMPTS, "DNI")
        state["user_data"] = user_data
        state["dni_attempts"] = 0
        self._event("user_data_retrieved", state)
        return self._result("ok", "Usuario localizado. Puede generar el código de verificación.", "identificacion")

    def generate_otp(self, state: dict) -> dict:
        if "user_data" not in state:
            return self._invalid_step("identificacion")
        state.pop("otp_pending", None)
        try:
            sent = self._retry(self.insurer.generate_otp, state["user_data"]["phone"])
        except RetryExhausted:
            return self._dependency_failure(state, "No se pudo generar el código.")
        if not sent:
            return self._dependency_failure(state, "No se pudo enviar el código.")
        state["otp_pending"] = True
        self._event("otp_generated", state)
        return self._result("ok", "Código simulado enviado al teléfono registrado.", "identificacion")

    def verify_otp(self, state: dict, verification_value: str) -> dict:
        if not state.get("otp_pending"):
            return self._invalid_step("identificacion")
        user_data = state["user_data"]
        try:
            verified_user_id = self._retry(
                self.insurer.verify_identity, user_data["phone"], verification_value
            )
        except RetryExhausted:
            return self._dependency_failure(state, "No se pudo comprobar el código.")
        if verified_user_id != user_data["user_id"]:
            return self._identity_input_failed(state, "otp_attempts", self.MAX_OTP_ATTEMPTS, "código")
        state.pop("otp_pending", None)
        state["otp_attempts"] = 0
        state["identity_verified"] = True
        self._event("identity_verified", state)
        return self._result("ok", "Identidad verificada. Describa el incidente.", "comprobacion")

    def categorize_incident(self, state: dict, data: dict) -> dict:
        if not state.get("identity_verified"):
            return self._invalid_step("comprobacion")
        for key in ("incident", "incident_category", "policy", "coverage_approved", "summary_text", "request_id"):
            state.pop(key, None)
        try:
            incident = Incident.model_validate(data)
        except ValidationError:
            return self._result("invalid_data", "Falta algún dato del incidente o tiene un formato inválido; aclare fecha, ubicación, tipo, descripción y daños.", "comprobacion")
        try:
            category = self._retry(self.insurer.categorize_incident, incident.description)
        except RetryExhausted:
            return self._dependency_failure(state, "No se pudo categorizar el incidente.")
        if category is None:
            return self._result("ambiguous_incident", "La descripción no permite determinar una única clase; aclare lo ocurrido.", "comprobacion")
        if category != incident.incident_type:
            return self._result("contradictory_incident", "El tipo indicado contradice la descripción; aclare lo ocurrido.", "comprobacion")
        state["incident"] = incident.model_dump(mode="json")
        state["incident_category"] = category
        self._event("incident_categorized", state)
        return self._result("ok", "Incidente categorizado. Consulte la póliza.", "comprobacion", category=category)

    def retrieve_policy(self, state: dict) -> dict:
        if not state.get("incident_category"):
            return self._invalid_step("comprobacion")
        try:
            policy = self._retry(
                self.insurer.get_policy, state["user_data"]["policy_lookup_key"]
            )
        except RetryExhausted:
            return self._dependency_failure(state, "No se pudo consultar la póliza.")
        if policy is None:
            return self._result("uncovered", "No se encontró una póliza aplicable; aclare los datos o solicite atención humana.", "comprobacion")
        state["policy"] = policy.model_dump(mode="json")
        try:
            covered, reason = self._retry(
                self.insurer.check_coverage,
                policy,
                state["incident_category"],
                Incident.model_validate(state["incident"]).incident_date,
            )
        except RetryExhausted:
            return self._dependency_failure(state, "No se pudo comprobar la cobertura.")
        state["coverage_approved"] = covered
        self._event("coverage_checked", state)
        if not covered:
            return self._result("uncovered", reason + " Puede aclarar o corregir el incidente.", "comprobacion")
        incident = state["incident"]
        state["summary_text"] = (
            f"Fecha: {incident['incident_date']}; ubicación: {incident['location']}; "
            f"tipo: {state['incident_category']}; descripción: {incident['description']}; "
            f"daños: {incident['damages']}."
        )
        return self._result("ok", "Cobertura comprobada. Presente el resumen y pida aceptación explícita.", "resumen")

    def open_claim(self, state: dict) -> dict:
        if state.get("claim"):
            self._set_closing_message(state, already_registered=True)
            return self._result("ok", "Parte ya registrado.", "cierre", claim=state["claim"])
        if not state.get("coverage_approved"):
            return self._invalid_step("apertura")
        request_id = state.setdefault("request_id", str(uuid4()))
        payload = {
            "user_id": state["user_data"]["user_id"],
            "policy_id": state["policy"]["policy_id"],
            **state["incident"],
        }
        fingerprint = claim_fingerprint(payload)
        # La huella identifica posibles duplicados; cada alta candidata necesita
        # un ID independiente para reconocer cuándo SQLite devuelve otra ya existente.
        claim_id = "CLM-" + uuid4().hex[:12].upper()
        created_at = datetime.now(timezone.utc).isoformat()
        try:
            claim = self._retry(self.repository.create, request_id, claim_id, created_at, payload, fingerprint)
        except RetryExhausted:
            # Una escritura puede haberse confirmado justo antes de fallar la respuesta.
            try:
                claim = self._retry(self.repository.get_by_request_id, request_id)
            except RetryExhausted:
                claim = None
            if claim is None:
                return self._result("dependency_error", "No se pudo confirmar el alta. Reintente o solicite atención humana.", "apertura")
        already_registered = claim["claim_id"] != claim_id
        state["claim"] = claim
        self._set_closing_message(state, already_registered=already_registered)
        self._event("claim_reused" if already_registered else "claim_created", state)
        message = "Parte ya registrado." if already_registered else "Parte registrado correctamente."
        return self._result("ok", message, "cierre", claim=claim)

    @staticmethod
    def _set_closing_message(state: dict, *, already_registered: bool) -> None:
        opening = (
            "El parte ya estaba registrado."
            if already_registered else "El parte ha quedado registrado."
        )
        state["closing_message"] = (
            f"{opening} Su identificador es {state['claim']['claim_id']}. "
            "Si quiere, anótelo para consultar su estado en la web."
        )

    def _identity_input_failed(self, state: dict, counter: str, limit: int, field: str) -> dict:
        attempts = state.get(counter, 0) + 1
        state[counter] = attempts
        route = "derivacion" if attempts >= limit else "identificacion"
        if route == "derivacion":
            state.pop("otp_pending", None)
            state.pop("user_data", None)
            return self._result("identity_failed", f"No se pudo verificar el {field} tras {limit} intentos. Se requiere atención humana.", route)
        return self._result("identity_failed", f"No se pudo verificar el {field}. Pida que lo repitan e inténtelo de nuevo.", route)

    def _dependency_failure(self, state: dict, message: str) -> dict:
        state.pop("otp_pending", None)
        return self._result("dependency_error", message + " Se requiere atención humana.", "derivacion")

    def _invalid_step(self, route: str) -> dict:
        return self._result("invalid_step", "Esta acción no está permitida en la etapa actual.", route)

    def _result(self, status: str, message: str, route: str, **extra) -> dict:
        if status != "ok":
            self.logger.info("claim_outcome status=%s route=%s", status, route)
        return {"status": status, "route": route, "message": message, **extra}

    def _event(self, event: str, state: dict) -> None:
        self.logger.info("claim_event=%s request_id=%s", event, state.get("request_id", "pending"))

    def _retry(self, operation, *args):
        for attempt in range(3):
            try:
                return operation(*args)
            except Exception:
                # Los fixtures no contienen datos reales; el mensaje no añade argumentos.
                self.logger.exception("Fallo en %s (intento %d/3)", operation.__name__, attempt + 1)
        raise RetryExhausted(operation.__name__)
