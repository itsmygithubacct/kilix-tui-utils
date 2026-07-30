"""Typed user-facing failures for stable CLI exit codes and JSON envelopes."""
from __future__ import annotations


class ResumeError(RuntimeError):
    code = "EFAIL"
    exit_status = 1


class UsageError(ResumeError):
    code = "EUSAGE"
    exit_status = 2


class NotFoundError(ResumeError):
    code = "ENOENT"
    exit_status = 3


class ConflictError(ResumeError):
    code = "ECONFLICT"
    exit_status = 4


class BackendError(ResumeError):
    code = "EBACKEND"
    exit_status = 5


class PacingError(ResumeError):
    code = "EPACING"
    exit_status = 6
