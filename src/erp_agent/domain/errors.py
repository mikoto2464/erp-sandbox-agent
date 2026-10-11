class ERPError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class Cancelled(ERPError):
    def __init__(self) -> None:
        super().__init__("cancelled", "任务已取消。", 409)
