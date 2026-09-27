"""Datos validados para el prototipo de apertura de siniestros."""

from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


IncidentCategory = Literal[
    "colision", "robo", "incendio", "danos_por_agua", "rotura_de_lunas"
]


class Incident(BaseModel):
    incident_date: date
    location: str = Field(min_length=3)
    incident_type: IncidentCategory
    description: str = Field(min_length=5)
    damages: str = Field(min_length=3)

    @field_validator("location", "description", "damages")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("El dato no puede estar vacío")
        return value

    @model_validator(mode="after")
    def valid_date(self):
        if self.incident_date > date.today():
            raise ValueError("La fecha del incidente no puede ser futura")
        return self


class Policy(BaseModel):
    policy_id: str
    active: bool
    coverages: frozenset[IncidentCategory]
    valid_from: date


@dataclass(frozen=True, slots=True)
class VoiceAgentConfig:
    deepgram_api_key: str
    llm_model: str
    ollama_endpoint: str


SAMPLE_RATE = 16000
