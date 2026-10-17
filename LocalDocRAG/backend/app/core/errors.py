class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details=None):
        self.code, self.message, self.status, self.details = code, message, status, details
        super().__init__(message)


def public_error(exc: Exception) -> dict:
    if isinstance(exc, AppError):
        return {"code": exc.code, "message": exc.message, "details": exc.details}
    # Do not expose paths, documents, model prompts or provider response bodies.
    return {"code": "PROCESSING_FAILED", "message": "处理失败，请检查本机服务并重试。", "details": {"type": type(exc).__name__}}

