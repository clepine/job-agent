"""Job sources. Each module exposes fetch(...) -> list[Job]."""

from . import ashby, github_repos, greenhouse, lever, smartrecruiters, workday  # noqa: F401

__all__ = ["ashby", "github_repos", "greenhouse", "lever", "smartrecruiters", "workday"]
