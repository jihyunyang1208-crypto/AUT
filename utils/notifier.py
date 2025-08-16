# utils/notifier.py
from core.ports import NotifierPort

class PrintNotifier(NotifierPort):
    def info(self, msg: str) -> None: print(msg)
    def warn(self, msg: str) -> None: print("WARN:", msg)
    def error(self, msg: str) -> None: print("ERROR:", msg)
