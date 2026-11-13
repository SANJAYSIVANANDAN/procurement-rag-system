from covenantiq.core.errors import (
    CovenantError,
    ErrorCode,
    NotFound,
    RateLimited,
)


def test_codes_are_class_level() -> None:
    assert NotFound.code is ErrorCode.NOT_FOUND
    assert CovenantError.code is ErrorCode.INTERNAL_ERROR


def test_to_dict_shape() -> None:
    error = NotFound("Document not found", details={"document_id": "d1"})
    assert error.to_dict() == {
        "code": "NOT_FOUND",
        "message": "Document not found",
        "details": {"document_id": "d1"},
    }


def test_rate_limited_carries_retry_after() -> None:
    error = RateLimited("slow down", retry_after=30)
    assert error.retry_after == 30
    assert isinstance(error, CovenantError)
