"""Las seis tools del diagrama, adaptadas a Pipecat Flows."""

import logging

from pipecat.flows.manager import FlowManager
from pipecat.flows.types import TRANSITION_IN_YAML

from .claims import ClaimsService


class FlowTools:
    def __init__(self, service: ClaimsService, logger: logging.Logger):
        self.service = service
        self.logger = logger

    async def recuperar_datos_usuario(self, flow_manager: FlowManager, document_id: str):
        """Busca un DNI sintético y guarda los datos de usuario solo en el estado privado."""
        return self._route(self.service.retrieve_user_data(flow_manager.state, document_id))

    async def generar_otp(self, flow_manager: FlowManager):
        """Simula el envío de un OTP al teléfono privado del usuario localizado."""
        return self._route(self.service.generate_otp(flow_manager.state))

    async def validar_otp(self, flow_manager: FlowManager, verification_value: str):
        """Valida el código con el teléfono privado y devuelve ACK o NACK."""
        return self._route(self.service.verify_otp(flow_manager.state, verification_value))

    async def categorizar_incidente(
        self, flow_manager: FlowManager, incident_date: str, location: str,
        incident_type: str, description: str, damages: str,
    ):
        """Valida los cinco datos del incidente y asigna una clase mock."""
        result = self.service.categorize_incident(flow_manager.state, {
            "incident_date": incident_date, "location": location,
            "incident_type": incident_type, "description": description, "damages": damages,
        })
        return self._route(result)

    async def recuperar_poliza(self, flow_manager: FlowManager):
        """Consulta la póliza privada y comprueba en código la cobertura de la clase."""
        return self._route(self.service.retrieve_policy(flow_manager.state))

    async def crear_parte(self, flow_manager: FlowManager):
        """Guarda un parte confirmado e idempotente en la BBDD mock."""
        return self._route(self.service.open_claim(flow_manager.state))

    @staticmethod
    def _route(result: dict):
        return result, TRANSITION_IN_YAML
