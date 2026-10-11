import json
import logging
import sys

from backend.config import sanitize
from backend.storage import now


class JsonFormatter(logging.Formatter):
    def __init__(self, secret: str | tuple[str, ...]):
        super().__init__()
        self.secret = secret

    def format(self, record: logging.LogRecord) -> str:
        message = sanitize(record.getMessage(), self.secret)
        return json.dumps(
            {"time": now(), "level": record.levelname, "logger": record.name, "message": message}
        )


def configure_logging(secret: str | tuple[str, ...]):
    logger = logging.getLogger("backend")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(secret))
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
