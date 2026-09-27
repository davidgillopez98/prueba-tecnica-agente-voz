# Prototipo de apertura de siniestros

La parte 2 implementa con datos sintéticos **identificación → comprobación → resumen → apertura → cierre**, representados en `docs/flujo_apertura_siniestros.drawio` y su PNG. `derivacion` es una salida excepcional para terminar la gestión automática o solicitar atención humana. El canal de voz usa FastAPI WebSocket y Pipecat. La conexión real con Genesys, un SMS y los sistemas de una aseguradora no forman parte del prototipo.

## Ejecución local

Requiere Python 3.13 y `uv`. Desde `fase 2`:

```powershell
uv sync
uv run python demo.py
uv run python demo.py --scenario invalid
uv run python demo.py --scenario uncovered
uv run python demo.py --scenario contradictory
uv run python demo.py --scenario correction
uv run python demo.py --scenario duplicate
uv run python demo.py --scenario failure
uv run python demo.py --scenario dni_correction
```

`demo.py` ejecuta por defecto los siete caminos del enunciado (`--scenario all`): `happy`, `invalid`, `uncovered`, `contradictory`, `correction`, `duplicate` y `failure`. `dni_correction` queda como escenario adicional. Usa el mismo harness que los tests E2E: envía audio sintético al WebSocket real y ejecuta el pipeline, Flow y FlowManager de Pipecat. Los mocks deterministas de STT, LLM y TTS sustituyen solo los proveedores externos; las tools y transiciones se ejecutan realmente. El caso `duplicate` abre dos sesiones sobre la misma SQLite y comprueba que ambas devuelven el mismo ID sin insertar otro expediente. `failure` simula tres fallos de escritura y comprueba que no se anuncia un alta. La elección de acciones del LLM está programada en cada escenario; la demo no evalúa la comprensión de un modelo real. Requiere las dependencias de desarrollo (`uv sync` incluye `pytest`). **Antes de cada escenario, `demo.py` elimina la SQLite indicada por `--db` (por defecto `claims.sqlite3`); se pierden los partes de ejecuciones anteriores.** Los escenarios de `--scenario all` quedan aislados; solo las dos sesiones de `duplicate` comparten la base de ese escenario. Los fixtures son `123456`/`111111` con cobertura de colisión, `SYN-200`/`222222` con póliza inactiva, `SYN-300`/`333333` con cobertura de robo y daños por agua, y `SYN-999` inexistente. El OTP y su envío son simulados.

## Etapas, tools y transiciones

| Etapa           | Tool                        | Entrada y efecto                                                                                                                                  |
| --------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Identificación | `recuperar_datos_usuario` | DNI sintético → consulta User data service y guarda`user_id`, teléfono y nombre en estado privado. Solo devuelve ACK/NACK al LLM.            |
| Identificación | `generar_otp`             | Lee el teléfono privado, simula el envío y devuelve ACK/NACK.                                                                                   |
| Identificación | `validar_otp`             | Envía teléfono privado + código dictado al mock. Solo su ACK pasa a comprobación.                                                             |
| Comprobación   | `categorizar_incidente`   | Valida fecha, ubicación, tipo, descripción y daños; devuelve una clase o solicita aclaración.                                                 |
| Comprobación   | `recuperar_poliza`        | Recupera la póliza del usuario privado y comprueba en código actividad, vigencia y cobertura de la clase. Solo su ACK pasa a resumen.           |
| Resumen         | `confirmar_resumen`       | Transición declarativa sin handler: solo tras la aceptación del usuario pasa a apertura. Un rechazo o corrección usa`volver_a_comprobacion`. |
| Apertura        | `crear_parte`             | Guarda en SQLite. Solo el ACK comprobado pasa a cierre.                                                                                           |

El clasificador mock reconoce `colision`, `robo`, `incendio`, `danos_por_agua` y `rotura_de_lunas`. Una descripción ambigua o contradictoria permanece en comprobación. Los fallos de DNI y de OTP se cuentan por separado en `FlowManager.state`: los dos primeros piden repetir el dato y el tercero deriva a atención humana. Un DNI válido reinicia el contador de DNI; corregir el DNI invalida el OTP anterior y reinicia su contador. En resumen, un rechazo o cambio llama a `volver_a_comprobacion`, una función `transition_only` sin handler Python. La siguiente clasificación sustituye el incidente y descarta la póliza y la cobertura anteriores; después se repiten las dos tools de comprobación. `confirmar_resumen` tampoco es una tool de negocio: es la arista de la máquina de estados que representa la aceptación.

Al entrar en resumen, una acción `tts_say` comunica el resumen exacto y pide confirmación. El nodo espera al usuario: con aceptación explícita, `confirmar_resumen` lleva a apertura; con rechazo, `volver_a_comprobacion` permite corregir y verificar de nuevo. `crear_parte` solo está disponible en apertura. El cierre comunica mediante `tts_say` el ID devuelto por SQLite y sugiere anotarlo para una futura consulta de estado en la web. El prototipo no implementa esa web. Cada una de las cinco etapas ofrece `derivar_a_humano`, otra función `transition_only`; el prototipo no afirma haber transferido técnicamente la llamada en Genesys.

Cada operación mock dispone de **tres intentos en total** si lanza una excepción, con `logging.exception` por intento. Esos reintentos técnicos no incrementan los contadores de DNI u OTP. Un NACK normal se devuelve sin repetir la misma operación. Tras tres excepciones de una dependencia se toma la salida de fallo sin anunciar una identidad, cobertura o alta inexistente.

### Persistencia e idempotencia

Los partes se guardan en una tabla SQLite con ID, fecha de alta, estado inicial `abierto` y datos del incidente. **Supuesto del prototipo:** usuario + póliza + fecha del incidente + ubicación + tipo identifican un único parte. La huella de esos campos evita duplicados sin límite de tiempo: un reintento devuelve el mismo ID y conserva el relato original, aunque cambien la descripción o los daños. La ubicación se compara literalmente. Dos incidentes reales con esa misma combinación quedarían agrupados; en producción haría falta una clave de evento más precisa.

La demo borra la SQLite antes de cada escenario; las dos sesiones de `duplicate` comparten la base para demostrar la idempotencia. El canal interactivo conserva los expedientes, pero pierde el estado de conversación al cerrar el WebSocket. Una base creada con el esquema anterior requiere una ruta nueva o reiniciar los datos sintéticos con la demo.

## Privacidad

El objeto devuelto por User data service y la póliza viven en `FlowManager.state`, fuera de `LLMContext`. Las tools de OTP y póliza leen de ese estado; el LLM no recibe teléfono, `user_id`, identificador de póliza ni código OTP desde los mocks. Los resultados de tools y los logs de aplicación tampoco incluyen esos datos. El resumen contiene únicamente los datos del incidente aportados por el usuario.

## Tests

```powershell
uv run --no-sync python -m pytest -q -p no:cacheprovider --basetemp .test-tmp
```

La invocación como módulo evita el error `uv trampoline failed to canonicalize script path` del ejecutable `pytest.exe` en Windows. Los tests unitarios cubren las precondiciones, privacidad de resultados, sustitución de OTP, clasificación, cobertura, idempotencia y tres intentos ante excepciones. `tests/e2e/test_flows.py` atraviesa WebSocket, pipeline, Flow y FlowManager reales de Pipecat y comprueba la transición del resumen, el ID hablado y caminos de error. También repite la misma alta en dos sesiones y verifica que ambas comunican el mismo ID y que SQLite contiene una sola fila. Sustituye STT, LLM, TTS y VAD por dobles deterministas, sin servicios de pago. La decisión conversacional de llamar a `confirmar_resumen` depende del LLM; estos E2E prueban las transiciones con llamadas programadas, no la interpretación de respuestas de un modelo real.

## Canal de voz interactivo opcional

`client.py` se conecta a `/ws` con PCM16 mono a 16 kHz. Para usar Deepgram STT/TTS y Ollama local, configura `.env.local` a partir de `.env.example` y ejecuta `uv run uvicorn src.main:app --host 127.0.0.1 --port 8000`, seguido de `uv run python client.py`. La demo y los tests no requieren esos proveedores.

El cliente envía bloques PCM por WebSocket. `AudioFrameSerializer` los convierte en `InputAudioRawFrame`; VAD y STT producen turnos transcritos para el agregador de contexto y el LLM. Las llamadas a tools actualizan `FlowManager.state` y cambian de nodo según `flow.json`. TTS convierte las respuestas y los `tts_say` de entrada en audio; el serializador devuelve `OutputAudioRawFrame` al cliente. `ContextInterruptionHandler` ajusta el último mensaje del asistente cuando el usuario interrumpe durante la locución. Tras una interrupción la conversación continúa en el mismo WebSocket; al cerrarlo se pierde el estado de esa sesión, sin reanudación entre conexiones.

## Organización

- `src/main.py`, `src/settings.py`: aplicación y configuración.
- `src/api/workflows/voice_agents/router.py`: WebSocket e inyección de dependencias.
- `src/api/workflows/voice_agents/ws_workflow/workflow.py`, `flow.json`, `tools.py`: pipeline, prompts y seis tools.
- `claims.py`, `schemas.py`: reglas de estado y validaciones.
- `mocks/services.py`: usuario, OTP, clases de incidente y pólizas sintéticas.
- `mocks/repository.py`: SQLite e idempotencia.
- `demo.py`, `tests/unit`, `tests/e2e`: recorridos reproducibles.

El prototipo no despliega Azure, no integra Genesys ni envía SMS reales. Usa únicamente datos ficticios.
