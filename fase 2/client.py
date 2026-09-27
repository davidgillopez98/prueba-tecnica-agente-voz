import argparse
import asyncio
import logging
import sys
import websockets
import sounddevice as sd
from src.api.common.logger import configure_logger, get_logger
from src.settings import get_settings

SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
CHUNK_SIZE = 800  # 50 ms a 16kHz (800 samples = 1600 bytes)


def list_devices():
    """Muestra la lista de dispositivos de audio disponibles en el sistema."""
    devices = sd.query_devices()
    print("\n========== DISPOSITIVOS DE ENTRADA (MICRÓFONOS) ==========")
    for idx, d in enumerate(devices):
        if d["max_input_channels"] > 0:
            print(f"[{idx:>2}] {d['name']} (canales: {d['max_input_channels']})")

    print("\n========== DISPOSITIVOS DE SALIDA (AURICULARES / ALTAVOCES) ==========")
    for idx, d in enumerate(devices):
        if d["max_output_channels"] > 0:
            print(f"[{idx:>2}] {d['name']} (canales: {d['max_output_channels']})")
    print("====================================================================\n")


def resolve_device(
    device_arg: str | None,
    query_name: str,
    is_input: bool = True,
    *,
    logger: logging.Logger,
) -> tuple[int, str]:
    """Resuelve el ID y nombre del dispositivo de audio."""
    devices = sd.query_devices()

    # 1. Si el usuario pasó un ID numérico directo
    if device_arg is not None:
        try:
            idx = int(device_arg)
            d = devices[idx]
            channels = d["max_input_channels"] if is_input else d["max_output_channels"]
            if channels > 0:
                return idx, d["name"]
            logger.warning(
                f"El dispositivo {idx} ({d['name']}) no tiene canales de {'entrada' if is_input else 'salida'}."
            )
        except (ValueError, IndexError):
            logger.warning(
                f"El argumento de dispositivo '{device_arg}' no es un índice válido. Buscando por nombre..."
            )

    # 2. Buscar por nombre exacto o parcial (ej: '2- arctis nova pro')
    query = (device_arg or query_name).lower()
    for idx, d in enumerate(devices):
        channels = d["max_input_channels"] if is_input else d["max_output_channels"]
        if channels > 0 and query in d["name"].lower():
            return idx, d["name"]

    # 3. Buscar por 'arctis' genérico si la búsqueda anterior falló
    for idx, d in enumerate(devices):
        channels = d["max_input_channels"] if is_input else d["max_output_channels"]
        if channels > 0 and "arctis" in d["name"].lower():
            return idx, d["name"]

    # 4. Fallback al dispositivo por defecto del sistema
    default_idx = sd.default.device[0 if is_input else 1]
    return default_idx, devices[default_idx]["name"]


async def run_client(
    ws_url: str,
    input_device_id: int,
    input_device_name: str,
    output_device_id: int,
    output_device_name: str,
    logger: logging.Logger,
):
    """Bucle principal de conexión WebSocket y streaming de audio."""
    logger.info(f"Conectando a {ws_url}...")
    logger.info(f"🎤 Micrófono : [{input_device_id}] {input_device_name}")
    logger.info(f"🎧 Auriculares: [{output_device_id}] {output_device_name}")

    send_queue = asyncio.Queue()
    playback_queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    # Callback de captura de micrófono
    def audio_input_callback(indata, frames, time_info, status):
        if status:
            logger.debug(f"Audio input status: {status}")
        # Encolar los bytes crudos (PCM int16)
        loop.call_soon_threadsafe(send_queue.put_nowait, bytes(indata))

    # Tarea de envío de audio al WebSocket
    async def send_audio_loop(ws):
        total_sent = 0
        try:
            while True:
                data = await send_queue.get()
                await ws.send(data)
                total_sent += len(data)
                send_queue.task_done()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("Envío de audio finalizado; bytes=%s", total_sent)

    # Tarea de recepción de audio desde el WebSocket
    async def receive_audio_loop(ws):
        total_received = 0
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    total_received += len(message)
                    await playback_queue.put(message)
                elif isinstance(message, str):
                    logger.debug("Mensaje de texto recibido del servidor; caracteres=%s", len(message))
        except asyncio.CancelledError:
            pass
        except websockets.exceptions.ConnectionClosed:
            logger.info("Conexión WebSocket cerrada por el servidor.")
        finally:
            logger.info("Recepción de audio finalizada; bytes=%s", total_received)

    # Tarea de reproducción de audio hacia el altavoz/auriculares
    def playback_worker(out_stream):
        while True:
            future = asyncio.run_coroutine_threadsafe(playback_queue.get(), loop)
            try:
                data = future.result()
                if data is None:
                    break
                out_stream.write(data)
                loop.call_soon_threadsafe(playback_queue.task_done)
            except Exception as e:
                logger.debug(f"Error en reproducción de audio: {e}")
                break

    try:
        async with websockets.connect(
            ws_url, max_size=None, ping_interval=20, ping_timeout=20
        ) as ws:
            logger.info("Conectado al servidor Pipecat WebSocket")
            print("\n" + "=" * 60)
            print("  ¡ASISTENTE DE VOZ LISTO!")
            print("  Habla por tus Arctis Nova Pro cuando quieras.")
            print("  Presiona Ctrl + C para salir.")
            print("=" * 60 + "\n")

            # Abrir streams de audio
            in_stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=CHUNK_SIZE,
                device=input_device_id,
                channels=CHANNELS,
                dtype=DTYPE,
                callback=audio_input_callback,
            )

            out_stream = sd.RawOutputStream(
                samplerate=SAMPLE_RATE,
                device=output_device_id,
                channels=CHANNELS,
                dtype=DTYPE,
            )

            with in_stream, out_stream:
                logger.info("Dispositivos de audio abiertos; streaming iniciado")
                playback_thread_task = loop.run_in_executor(
                    None, playback_worker, out_stream
                )
                sender_task = asyncio.create_task(send_audio_loop(ws))
                receiver_task = asyncio.create_task(receive_audio_loop(ws))

                done, pending = await asyncio.wait(
                    [sender_task, receiver_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for t in pending:
                    t.cancel()

                # Notificar al worker de playback para terminar
                await playback_queue.put(None)
                await playback_thread_task
                logger.info("Streaming de audio detenido")

    except websockets.exceptions.InvalidURI:
        logger.error(f"URL de WebSocket inválida: {ws_url}")
    except (ConnectionRefusedError, OSError) as e:
        logger.error(
            f"No se pudo conectar al servidor en {ws_url}. ¿Está la API en ejecución? ({e})"
        )


def main():
    configure_logger(get_settings().log_level)
    logger = get_logger()
    parser = argparse.ArgumentParser(
        description="Cliente de audio por WebSocket para Pipecat Voice Agent"
    )
    parser.add_argument(
        "--url",
        default="ws://127.0.0.1:8000/ws",
        help="URL WebSocket del servidor (por defecto: ws://127.0.0.1:8000/ws)",
    )
    parser.add_argument(
        "--mic",
        default=None,
        help="ID o nombre del micrófono (por defecto: búsqueda automática de Arctis Nova Pro 2)",
    )
    parser.add_argument(
        "--speaker",
        default=None,
        help="ID o nombre de los auriculares/altavoz (por defecto: búsqueda automática de Arctis Nova Pro 2)",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Listar todos los dispositivos de audio disponibles y salir",
    )

    args = parser.parse_args()

    if args.list_devices:
        list_devices()
        return

    in_id, in_name = resolve_device(
        args.mic, "2- arctis nova pro", is_input=True, logger=logger
    )
    out_id, out_name = resolve_device(
        args.speaker, "2- arctis nova pro", is_input=False, logger=logger
    )

    try:
        asyncio.run(
            run_client(
                ws_url=args.url,
                input_device_id=in_id,
                input_device_name=in_name,
                output_device_id=out_id,
                output_device_name=out_name,
                logger=logger,
            )
        )
    except KeyboardInterrupt:
        print("\n\nSesión finalizada por el usuario.")


if __name__ == "__main__":
    main()
