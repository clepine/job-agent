"""Regression: the pre-email description top-up was a silent no-op.

Descriptions are not persisted in the committed ledger, so any posting picked
from the scored backlog arrives at the email with an empty body. run.py tops
those up over free HTTP just before rendering - except it handed hydrate_all()
a list of database-loaded jobs, whose `needs_hydration` flag is False because
only the fetchers ever set it. The call reported "re-hydrated 0/5" and fetched
nothing, every run.

Downstream that is not a blank field, it is wrong content: compress_jd("") is
empty, so the keyword diff finds nothing and the card prints "ATS keywords:
none detected" as though the posting asked for no skills at all.
"""

from __future__ import annotations

from pipeline.models import Job
from sources import hydrate


def _job(description: str = "", ats: str = "greenhouse") -> Job:
    return Job(
        company="Acme",
        title="Software Engineer, New Grad",
        url="https://boards.greenhouse.io/acme/jobs/1",
        ats=ats,
        description=description,
    )


def test_a_job_loaded_from_the_database_is_flagged_for_rehydration():
    """The whole bug in one assertion: it arrives False and must not stay False."""
    job = _job()
    assert job.needs_hydration is False
    assert hydrate.mark_for_rehydration([job]) == [job]
    assert job.needs_hydration is True


def test_hydrate_all_actually_targets_what_mark_selected():
    """mark_for_rehydration and hydrate_all must agree on what needs fetching."""
    job = _job()
    assert hydrate.hydrate_all(None, [job]) == (0, 0)  # unflagged: no-op
    hydrate.mark_for_rehydration([job])
    attempted, _ok = hydrate.hydrate_all(_FailingClient(), [job])
    assert attempted == 1


def test_a_job_that_already_has_a_body_is_left_alone():
    job = _job(description="<p>Requirements: Python, C++</p>")
    assert hydrate.mark_for_rehydration([job]) == []
    assert job.needs_hydration is False


def test_a_source_with_no_body_endpoint_is_not_selected():
    """hydratable() gates on the ATS; flagging an unfetchable job would only
    inflate the attempted count with guaranteed failures."""
    job = _job(ats="other")
    assert hydrate.mark_for_rehydration([job]) == []
    assert job.needs_hydration is False


class _FailingClient:
    """Hydration is best-effort, so a failing client still counts as attempted."""

    def get(self, *_a, **_kw):
        raise RuntimeError("network disabled in tests")

    def post(self, *_a, **_kw):
        raise RuntimeError("network disabled in tests")
