"""Unit tests for the SDK-exception → HTTP-error mapping."""

from __future__ import annotations

import pytest

from indiek import ArchiveRuleError, AuthorshipError, IntegrityError, UnknownItemError

from indiek_api import errors


@pytest.mark.parametrize(
    ("raised", "status", "code"),
    [
        (IntegrityError("Chain broken!"), 409, "archive_integrity"),
        (AuthorshipError("Only the tag's author may use this operation"), 403, "forbidden"),
        (UnknownItemError("Unknown ID or alias 'e99'"), 404, "item_not_found"),
        (ArchiveRuleError("Tag kind mismatch"), 422, "archive_rule"),
        (ValueError("bad input"), 422, "archive_rule"),
    ],
)
def test_library_errors_are_classified_by_type(raised: Exception, status: int, code: str) -> None:
    with pytest.raises(errors.IndieKAPIError) as caught:
        with errors.library_errors():
            raise raised
    assert (caught.value.status_code, caught.value.code) == (status, code)
    assert caught.value.detail == str(raised)
    assert caught.value.__cause__ is raised


def test_unexpected_errors_propagate_as_bugs() -> None:
    with pytest.raises(KeyError):
        with errors.library_errors():
            raise KeyError("not a library failure")
