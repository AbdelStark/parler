"""Project-specific error hierarchy."""

from __future__ import annotations


class ParlerError(Exception):
    """Base class for all parler errors."""

    exit_code: int = 1


class InputError(ParlerError):
    exit_code = 2


class ConfigError(InputError):
    exit_code = 2


class EnvironmentError(ParlerError):
    exit_code = 3


class APIError(ParlerError):
    exit_code = 4


class ProcessingError(ParlerError):
    exit_code = 5


class OutputError(ParlerError):
    exit_code = 6


class ExportError(OutputError):
    exit_code = 6


def exit_code_for(error: BaseException) -> int:
    """Map an exception to the project exit code contract."""

    if isinstance(error, ParlerError):
        return error.exit_code
    return 1
