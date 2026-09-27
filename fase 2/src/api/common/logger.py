"""Logger compartido de la aplicación, basado en la biblioteca estándar."""

import logging
import sys

LOGGER_NAME = "test_pipecat"


def configure_logger(level: str = "INFO") -> logging.Logger:
    """Configura una única salida de consola para el logger de la aplicación."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    logger.propagate = False

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s"
        )
    )
    logger.addHandler(handler)
    return logger


def get_logger() -> logging.Logger:
    """Devuelve el logger con nombre configurado en el lifespan."""
    return logging.getLogger(LOGGER_NAME)
