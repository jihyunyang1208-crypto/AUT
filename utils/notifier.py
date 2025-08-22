# utils/notifier.py
from core.ports import NotifierPort
import logging
logger = logging.getLogger(__name__)
class PrintNotifier(NotifierPort):
    def info(self, msg: str) -> None: logger.debug(msg)
    def warn(self, msg: str) -> None: logger.debug("WARN:", msg)
    def error(self, msg: str) -> None: logger.debug("ERROR:", msg)
