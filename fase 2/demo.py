"""Demo reproducible del WebSocket y pipeline Pipecat con voz e inferencia simuladas."""

import argparse
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from loguru import logger as pipecat_logger

pipecat_logger.disable("pipecat")

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from src import main as main_module
from src.main import app
from src.settings import Settings, get_settings
from src.api.workflows.voice_agents.ws_workflow.mocks.repository import ClaimRepository
from src.api.workflows.voice_agents.ws_workflow.workflow import GREETING
from tests.e2e.service_mocks import LLMAction, MockVoiceServices, VoiceScenario


@dataclass(frozen=True)
class DemoCase:
    voice: VoiceScenario
    turns: tuple[bytes, ...]
    final_node: str
    expected_tools: tuple[str, ...]


REQUIRED_SCENARIOS = (
    "happy", "invalid", "uncovered", "contradictory", "correction", "duplicate", "failure",
)


def incident(**changes) -> dict:
    return {
        "incident_date": (date.today() - timedelta(days=1)).isoformat(),
        "location": "Madrid centro",
        "incident_type": "colision",
        "description": "Choque leve en cruce",
        "damages": "Paragolpes delantero",
    } | changes


def build_happy_case() -> DemoCase:
    """Identificación, cobertura y alta correcta del parte."""
    return DemoCase(
        voice=VoiceScenario(
            transcripts={
                b'dni': '123456',
                b'otp': '111111',
                b'incident': 'Choque en Madrid',
                b'confirm': 'Sí, confirmo',
            },
            llm_actions=(
                LLMAction(text=GREETING),
                LLMAction(tool='recuperar_datos_usuario', arguments={'document_id': '123456'}),
                LLMAction(tool='generar_otp'),
                LLMAction(text='Indique el código.'),
                LLMAction(tool='validar_otp', arguments={'verification_value': '111111'}),
                LLMAction(text='Describa el incidente.'),
                LLMAction(tool='categorizar_incidente', arguments=incident()),
                LLMAction(tool='recuperar_poliza'),
                LLMAction(tool='confirmar_resumen'),
                LLMAction(tool='crear_parte'),
            ),
        ),
        turns=(b'dni', b'otp', b'incident', b'confirm'),
        final_node='cierre',
        expected_tools=(
            'recuperar_datos_usuario',
            'generar_otp',
            'validar_otp',
            'categorizar_incidente',
            'recuperar_poliza',
            'crear_parte',
        ),
    )


def build_invalid_case() -> DemoCase:
    """Tres documentos inv?lidos terminan en atenci?n humana."""
    return DemoCase(
        voice=VoiceScenario(
            transcripts={
                b'bad1': 'SYN-999',
                b'bad2': 'SYN-999',
                b'bad3': 'SYN-999',
            },
            llm_actions=(
                LLMAction(text=GREETING),
                LLMAction(tool='recuperar_datos_usuario', arguments={'document_id': 'SYN-999'}),
                LLMAction(text='Repita el DNI.'),
                LLMAction(tool='recuperar_datos_usuario', arguments={'document_id': 'SYN-999'}),
                LLMAction(text='Repita el DNI por última vez.'),
                LLMAction(tool='recuperar_datos_usuario', arguments={'document_id': 'SYN-999'}),
                LLMAction(text='Se requiere atención humana.'),
            ),
        ),
        turns=(b'bad1', b'bad2', b'bad3'),
        final_node='derivacion',
        expected_tools=(
            'recuperar_datos_usuario',
            'recuperar_datos_usuario',
            'recuperar_datos_usuario',
        ),
    )


def build_uncovered_case() -> DemoCase:
    """La p?liza no cubre el incidente y no se crea un parte."""
    return DemoCase(
        voice=VoiceScenario(
            transcripts={
                b'dni': 'SYN-300',
                b'otp': '333333',
                b'incident': 'Choque en Madrid',
            },
            llm_actions=(
                LLMAction(text=GREETING),
                LLMAction(tool='recuperar_datos_usuario', arguments={'document_id': 'SYN-300'}),
                LLMAction(tool='generar_otp'),
                LLMAction(text='Indique el código.'),
                LLMAction(tool='validar_otp', arguments={'verification_value': '333333'}),
                LLMAction(text='Describa el incidente.'),
                LLMAction(tool='categorizar_incidente', arguments=incident()),
                LLMAction(tool='recuperar_poliza'),
                LLMAction(text='La póliza no cubre esa clase de incidente.'),
            ),
        ),
        turns=(b'dni', b'otp', b'incident'),
        final_node='comprobacion',
        expected_tools=(
            'recuperar_datos_usuario',
            'generar_otp',
            'validar_otp',
            'categorizar_incidente',
            'recuperar_poliza',
        ),
    )


def build_contradictory_case() -> DemoCase:
    """Se corrige el tipo de incidente antes de consultar la p?liza."""
    return DemoCase(
        voice=VoiceScenario(
            transcripts={
                b'dni': '123456',
                b'otp': '111111',
                b'incident': 'Ayer hubo un choque en Madrid, pero he indicado incendio',
                b'confirm': 'Sí, confirmo',
                b'corrected': 'Corrijo el tipo: fue una colisión',
            },
            llm_actions=(
                LLMAction(text=GREETING),
                LLMAction(tool='recuperar_datos_usuario', arguments={'document_id': '123456'}),
                LLMAction(tool='generar_otp'),
                LLMAction(text='Indique el código.'),
                LLMAction(tool='validar_otp', arguments={'verification_value': '111111'}),
                LLMAction(text='Describa el incidente.'),
                LLMAction(tool='categorizar_incidente', arguments=incident(incident_type='incendio')),
                LLMAction(text='El tipo indicado contradice la descripción. ¿Fue una colisión o un incendio?'),
                LLMAction(tool='categorizar_incidente', arguments=incident()),
                LLMAction(tool='recuperar_poliza'),
                LLMAction(tool='confirmar_resumen'),
                LLMAction(tool='crear_parte'),
            ),
        ),
        turns=(b'dni', b'otp', b'incident', b'corrected', b'confirm'),
        final_node='cierre',
        expected_tools=(
            'recuperar_datos_usuario',
            'generar_otp',
            'validar_otp',
            'categorizar_incidente',
            'categorizar_incidente',
            'recuperar_poliza',
            'crear_parte',
        ),
    )


def build_correction_case() -> DemoCase:
    """Se corrige la ubicaci?n y se vuelve a comprobar la cobertura."""
    return DemoCase(
        voice=VoiceScenario(
            transcripts={
                b'dni': '123456',
                b'otp': '111111',
                b'incident': 'Choque en Madrid',
                b'confirm': 'Sí, confirmo',
                b'correction': 'No, fue en Madrid norte',
                b'new_incident': 'Choque en Madrid norte',
            },
            llm_actions=(
                LLMAction(text=GREETING),
                LLMAction(tool='recuperar_datos_usuario', arguments={'document_id': '123456'}),
                LLMAction(tool='generar_otp'),
                LLMAction(text='Indique el código.'),
                LLMAction(tool='validar_otp', arguments={'verification_value': '111111'}),
                LLMAction(text='Describa el incidente.'),
                LLMAction(tool='categorizar_incidente', arguments=incident()),
                LLMAction(tool='recuperar_poliza'),
                LLMAction(tool='volver_a_comprobacion'),
                LLMAction(text='Corrijamos la ubicación.'),
                LLMAction(tool='categorizar_incidente', arguments=incident(location='Madrid norte')),
                LLMAction(tool='recuperar_poliza'),
                LLMAction(tool='confirmar_resumen'),
                LLMAction(tool='crear_parte'),
            ),
        ),
        turns=(b'dni', b'otp', b'incident', b'correction', b'new_incident', b'confirm'),
        final_node='cierre',
        expected_tools=(
            'recuperar_datos_usuario',
            'generar_otp',
            'validar_otp',
            'categorizar_incidente',
            'recuperar_poliza',
            'categorizar_incidente',
            'recuperar_poliza',
            'crear_parte',
        ),
    )


def build_duplicate_case(*, retry_variant: bool = False) -> DemoCase:
    """Dos solicitudes equivalentes deben conservar un ?nico parte."""
    duplicate_incident = incident(location="Sevilla")
    if retry_variant:
        duplicate_incident = incident(
            location="Sevilla",
            description="Accidente leve en un cruce",
            damages="Abolladura del parachoques",
        )
    return DemoCase(
        voice=VoiceScenario(
            transcripts={
                b'dni': '123456',
                b'otp': '111111',
                b'incident': duplicate_incident["description"],
                b'confirm': 'Sí, confirmo',
            },
            llm_actions=(
                LLMAction(text=GREETING),
                LLMAction(tool='recuperar_datos_usuario', arguments={'document_id': '123456'}),
                LLMAction(tool='generar_otp'),
                LLMAction(text='Indique el código.'),
                LLMAction(tool='validar_otp', arguments={'verification_value': '111111'}),
                LLMAction(text='Describa el incidente.'),
                LLMAction(tool="categorizar_incidente", arguments=duplicate_incident),
                LLMAction(tool='recuperar_poliza'),
                LLMAction(tool='confirmar_resumen'),
                LLMAction(tool='crear_parte'),
            ),
        ),
        turns=(b'dni', b'otp', b'incident', b'confirm'),
        final_node='cierre',
        expected_tools=(
            'recuperar_datos_usuario',
            'generar_otp',
            'validar_otp',
            'categorizar_incidente',
            'recuperar_poliza',
            'crear_parte',
        ),
    )


def build_failure_case() -> DemoCase:
    """El fallo de persistencia se comunica sin anunciar un alta."""
    return DemoCase(
        voice=VoiceScenario(
            transcripts={
                b'dni': '123456',
                b'otp': '111111',
                b'incident': 'Choque en Madrid',
                b'confirm': 'Sí, confirmo',
            },
            llm_actions=(
                LLMAction(text=GREETING),
                LLMAction(tool='recuperar_datos_usuario', arguments={'document_id': '123456'}),
                LLMAction(tool='generar_otp'),
                LLMAction(text='Indique el código.'),
                LLMAction(tool='validar_otp', arguments={'verification_value': '111111'}),
                LLMAction(text='Describa el incidente.'),
                LLMAction(tool='categorizar_incidente', arguments=incident()),
                LLMAction(tool='recuperar_poliza'),
                LLMAction(tool='confirmar_resumen'),
                LLMAction(tool='crear_parte'),
                LLMAction(text='No he podido confirmar el alta; puede reintentar o pedir atención humana.'),
            ),
        ),
        turns=(b'dni', b'otp', b'incident', b'confirm'),
        final_node='apertura',
        expected_tools=(
            'recuperar_datos_usuario',
            'generar_otp',
            'validar_otp',
            'categorizar_incidente',
            'recuperar_poliza',
            'crear_parte',
        ),
    )


def build_dni_correction_case() -> DemoCase:
    """La correcci?n del documento sustituye el reto OTP anterior."""
    return DemoCase(
        voice=VoiceScenario(
            transcripts={
                b'first': 'SYN-200',
                b'correction': 'El DNI correcto es 123456',
                b'otp': '111111',
                b'incident': 'Choque en Madrid',
                b'confirm': 'Sí, confirmo',
            },
            llm_actions=(
                LLMAction(text=GREETING),
                LLMAction(tool='recuperar_datos_usuario', arguments={'document_id': 'SYN-200'}),
                LLMAction(tool='generar_otp'),
                LLMAction(text='Indique el código o corrija el DNI.'),
                LLMAction(tool='recuperar_datos_usuario', arguments={'document_id': '123456'}),
                LLMAction(tool='generar_otp'),
                LLMAction(text='Indique el nuevo código.'),
                LLMAction(tool='validar_otp', arguments={'verification_value': '111111'}),
                LLMAction(text='Describa el incidente.'),
                LLMAction(tool='categorizar_incidente', arguments=incident()),
                LLMAction(tool='recuperar_poliza'),
                LLMAction(tool='confirmar_resumen'),
                LLMAction(tool='crear_parte'),
            ),
        ),
        turns=(b'first', b'correction', b'otp', b'incident', b'confirm'),
        final_node='cierre',
        expected_tools=(
            'recuperar_datos_usuario',
            'generar_otp',
            'recuperar_datos_usuario',
            'generar_otp',
            'validar_otp',
            'categorizar_incidente',
            'recuperar_poliza',
            'crear_parte',
        ),
    )


def build_case(name: str, *, retry_variant: bool = False) -> DemoCase:
    """Selecciona el guion completo del escenario solicitado."""
    builders = {
        'happy': build_happy_case,
        'invalid': build_invalid_case,
        'uncovered': build_uncovered_case,
        'contradictory': build_contradictory_case,
        'correction': build_correction_case,
        'duplicate': build_duplicate_case,
        'failure': build_failure_case,
        'dni_correction': build_dni_correction_case,
    }
    if name not in builders:
        raise ValueError(f"Escenario desconocido: {name}")
    if name == "duplicate":
        return build_duplicate_case(retry_variant=retry_variant)
    return builders[name]()


def receive_assistant(websocket) -> str:
    audio = websocket.receive_bytes().rstrip(b"\x00")
    prefix = b"VOICE:"
    if not audio.startswith(prefix):
        raise RuntimeError("El pipeline devolvió audio inesperado")
    message = audio[len(prefix):].decode("utf-8")
    print(f"Asistente: {message}", flush=True)
    return message


def claim_count(db_path: Path) -> int:
    with closing(sqlite3.connect(db_path)) as connection:
        return connection.execute("SELECT count(*) FROM claims").fetchone()[0]


def run_session(name: str, db_path: Path, *, retry_variant: bool = False):
    case = build_case(name, retry_variant=retry_variant)
    repository = ClaimRepository(db_path)
    before_count = claim_count(db_path)
    failed_writes = []
    with MonkeyPatch.context() as patch:
        mocks = MockVoiceServices(patch, db_path.parent, case.voice)
        if name == "failure":
            def unavailable(_repository, *_args):
                failed_writes.append(True)
                raise OSError("Fallo de persistencia simulado")

            patch.setattr(ClaimRepository, "create", unavailable)
        settings = Settings(
            _env_file=None,
            deepgram__api_key="fake-deepgram",
            llm__model="fake-model",
            llm__endpoint="localhost:11434/api",
            claims_db_path=db_path,
            log_level="WARNING",
        )
        patch.setattr(main_module, "get_settings", lambda: settings)
        patch.setattr(
            app,
            "dependency_overrides",
            {get_settings: lambda: settings},
        )
        with TestClient(app) as client:
            with client.websocket_connect("/ws") as websocket:
                first_message = receive_assistant(websocket)
                if first_message != GREETING:
                    raise RuntimeError("El saludo inicial no coincide con el configurado")
                for audio in case.turns:
                    print(f"Usuario: {case.voice.transcripts[audio]}", flush=True)
                    websocket.send_bytes(audio)
                    last_message = receive_assistant(websocket)

    if mocks.manager.current_node != case.final_node:
        raise RuntimeError(f"Etapa final inesperada: {mocks.manager.current_node}")
    if tuple(mocks.tool_calls) != case.expected_tools:
        raise RuntimeError(f"Tools inesperadas: {mocks.tool_calls}")
    if tuple(mocks.transcripts) != tuple(case.voice.transcripts[audio] for audio in case.turns):
        raise RuntimeError("El STT simulado no procesó todos los turnos")
    if len(mocks.llm_steps) != len(case.voice.llm_actions):
        raise RuntimeError("El LLM simulado no completó todas las inferencias")
    claim = mocks.manager.state.get("claim")
    if name in {"invalid", "uncovered", "failure"} and claim:
        raise RuntimeError("Se ha creado un parte sin cumplir las precondiciones")
    if name in {"invalid", "uncovered", "failure"} and claim_count(db_path) != before_count:
        raise RuntimeError("Se ha persistido un parte en un camino sin alta")
    if case.final_node == "cierre" and (not claim or claim["claim_id"] not in last_message):
        raise RuntimeError("El cierre no comunicó el identificador del parte")
    if claim and repository.get(claim["claim_id"]) is None:
        raise RuntimeError("El parte anunciado no se ha persistido")
    if name == "failure" and (len(failed_writes) != 3 or "identificador" in last_message.lower()):
        raise RuntimeError("El fallo de persistencia no se gestionó de forma segura")
    if name == "contradictory":
        expected = ("recuperar_datos_usuario", "generar_otp", "validar_otp",
                    "categorizar_incidente", "categorizar_incidente", "recuperar_poliza", "crear_parte")
        if tuple(mocks.tool_calls) != expected:
            raise RuntimeError("La póliza se consultó antes de corregir los datos")
    print(f"[Pipecat: {len(mocks.transcripts)} turnos STT, {len(mocks.llm_steps)} inferencias LLM, etapa {case.final_node}]")
    print(f"[Tools: {', '.join(mocks.tool_calls)}]")
    if name == "failure":
        print(f"[Persistencia: {len(failed_writes)} intentos fallidos; ningún parte creado]")
    return mocks


def run(name: str, db_path: Path) -> None:
    db_path = db_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Cada escenario empieza vacío; duplicate conserva la base entre sus dos sesiones.
    db_path.unlink(missing_ok=True)
    if name != "duplicate":
        run_session(name, db_path)
        return

    print("Solicitud inicial")
    first = run_session("duplicate", db_path)
    first_count = claim_count(db_path)
    print("Reintento de la misma solicitud en otra sesión")
    second = run_session("duplicate", db_path, retry_variant=True)
    if first.manager.state["claim"]["claim_id"] != second.manager.state["claim"]["claim_id"]:
        raise RuntimeError("El reintento ha generado otro identificador")
    if claim_count(db_path) != first_count:
        raise RuntimeError("El reintento ha creado otro expediente")
    stored = ClaimRepository(db_path).get(first.manager.state["claim"]["claim_id"])
    if stored["incident"]["description"] != first.manager.state["incident"]["description"]:
        raise RuntimeError("El reintento ha modificado el relato original")
    print(f"[Idempotencia: un único parte, {first.manager.state['claim']['claim_id']}]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Demo del pipeline Pipecat con STT, LLM y TTS simulados")
    parser.add_argument("--scenario", choices=("all", *REQUIRED_SCENARIOS, "dni_correction"), default="all")
    parser.add_argument("--db", type=Path, default=Path("claims.sqlite3"))
    args = parser.parse_args()
    scenarios = REQUIRED_SCENARIOS if args.scenario == "all" else (args.scenario,)
    for scenario in scenarios:
        print(f"\n=== {scenario} ===", flush=True)
        run(scenario, args.db)
