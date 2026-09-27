"""Servicios mock independientes, con datos exclusivamente sintéticos."""

import unicodedata
from dataclasses import dataclass
from datetime import date

from ..schemas import Policy


@dataclass(frozen=True)
class Customer:
    user_id: str
    phone: str
    verification_value: str
    display_name: str


class MockUserDataService:
    customers = {
        "123456": Customer("USR-100", "+34000000100", "111111", "Cliente A"),
        "SYN-200": Customer("USR-200", "+34000000200", "222222", "Cliente B"),
        "SYN-300": Customer("USR-300", "+34000000300", "333333", "Cliente C"),
    }

    def get_user_data(self, document_id: str) -> dict | None:
        customer = self.customers.get(document_id.strip().upper())
        if customer is None:
            return None
        return {
            "user_id": customer.user_id,
            "phone": customer.phone,
            "display_name": customer.display_name,
            "policy_lookup_key": customer.user_id,
        }


class MockOTPService:
    def __init__(self, customers: dict[str, Customer]):
        self.customers = customers

    def generate_otp(self, phone: str) -> bool:
        # El envío y el código son fixtures; no hay SMS real.
        return any(customer.phone == phone for customer in self.customers.values())

    def verify_identity(self, phone: str, verification_value: str) -> str | None:
        for customer in self.customers.values():
            if customer.phone == phone and customer.verification_value == verification_value:
                return customer.user_id
        return None


class MockIncidentCategorizer:
    keywords = {
        "colision": ("choque", "colision", "accidente", "impacto"),
        "robo": ("robo", "robado", "sustraccion"),
        "incendio": ("incendio", "fuego", "quemado"),
        "danos_por_agua": ("agua", "inundacion", "filtracion"),
        "rotura_de_lunas": ("luna", "cristal", "parabrisas"),
    }

    def categorize_incident(self, description: str) -> str | None:
        normalized = unicodedata.normalize("NFKD", description.casefold())
        normalized = "".join(character for character in normalized if not unicodedata.combining(character))
        matches = [
            category for category, words in self.keywords.items()
            if any(word in normalized for word in words)
        ]
        return matches[0] if len(matches) == 1 else None


class MockPolicyService:
    policies = {
        "USR-100": Policy(policy_id="POL-100", active=True, coverages=frozenset({"colision", "robo", "rotura_de_lunas"}), valid_from=date(2020, 1, 1)),
        "USR-200": Policy(policy_id="POL-200", active=False, coverages=frozenset({"colision"}), valid_from=date(2020, 1, 1)),
        "USR-300": Policy(policy_id="POL-300", active=True, coverages=frozenset({"robo", "danos_por_agua"}), valid_from=date(2020, 1, 1)),
    }

    def get_policy(self, user_id: str) -> Policy | None:
        return self.policies.get(user_id)

    def check_coverage(self, policy: Policy, incident_type: str, incident_date: date) -> tuple[bool, str]:
        if not policy.active:
            return False, "La póliza está inactiva."
        if incident_date < policy.valid_from:
            return False, "La fecha del incidente precede la vigencia de la póliza."
        if incident_type not in policy.coverages:
            return False, "La clase del incidente no está cubierta por la póliza."
        return True, "Cobertura comprobada."


class MockInsuranceServices:
    """Fachada para inyectar los cuatro servicios mock en ClaimsService."""

    def __init__(self):
        users = MockUserDataService()
        otp = MockOTPService(users.customers)
        categorizer = MockIncidentCategorizer()
        policies = MockPolicyService()
        self.get_user_data = users.get_user_data
        self.generate_otp = otp.generate_otp
        self.verify_identity = otp.verify_identity
        self.categorize_incident = categorizer.categorize_incident
        self.get_policy = policies.get_policy
        self.check_coverage = policies.check_coverage
