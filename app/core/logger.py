import sys
from loguru import logger


def configurar_logger():
    logger.remove()

    logger.add(
        sys.stdout,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
        level="INFO",
        serialize=True,
    )

    logger.add(
        "logs/atmos_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="7 days",
        level="WARNING",
        serialize=True,
    )

    return logger


log = configurar_logger()
