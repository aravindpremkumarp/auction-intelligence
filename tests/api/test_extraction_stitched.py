"""The review API on a stitched notice: leader shows the joined text, follower
points at the leader, and the stale badge sees the classification stamp."""
from __future__ import annotations

import inspect

from api.review import extraction as E


def test_rerun_worker_reads_the_stitched_text_and_refuses_followers():
    src = inspect.getsource(E._rerun_worker)
    assert "coalesce(d.stitched_markdown, d.markdown) AS md" in src
    assert "d.stitched_into AS stitched_into" in src
    assert "stitched into" in src   # the refusal message
