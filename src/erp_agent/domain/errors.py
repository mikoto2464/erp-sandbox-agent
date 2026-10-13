class ERPError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class Cancelled(ERPError):
    def __init__(self) -> None:
        super().__init__("cancelled", "任务已取消。", 409)


class ModelOutputTruncated(ERPError):
    def __init__(
        self,
        message: str,
        output_limit: int,
        configured_limit: int,
        output_tokens: int | None = None,
        reasoning_tokens: int | None = None,
    ) -> None:
        super().__init__("llm_incomplete", message, 502)
        self.output_limit = output_limit
        self.configured_limit = configured_limit
        self.output_tokens = output_tokens
        self.reasoning_tokens = reasoning_tokens
