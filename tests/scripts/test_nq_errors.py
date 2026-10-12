"""scripts.score_ink_coverage.nq must not report a refused write as done."""
import pytest

from scripts.score_ink_coverage import QueryError, _values


def test_rows_come_back_when_there_are_no_errors():
    assert _values({"data": {"values": [[1]]}}) == [[1]]


def test_a_refused_write_raises_even_with_data():
    """The relationship cap answers 200 with data AND errors."""
    refused = {"data": {"fields": ["linked"], "values": [[1000]]},
               "errors": [{"code": "Neo.ClientError.Transaction.TransactionHookFailed",
                           "message": "exceeded the logical size limit"}]}
    with pytest.raises(QueryError, match="TransactionHookFailed"):
        _values(refused)
