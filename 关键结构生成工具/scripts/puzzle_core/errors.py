"""Stable domain errors shared by the CLI and the future local web service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ErrorDetail:
    code: str
    message: str
    json_path: str | None = None
    evidence: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.json_path is not None:
            result["json_path"] = self.json_path
        if self.evidence:
            result["evidence"] = dict(self.evidence)
        return result


class PuzzleToolError(Exception):
    """Base exception carrying a stable machine-readable error code."""

    code = "PUZZLE_TOOL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        json_path: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.detail = ErrorDetail(self.code, message, json_path, evidence)


class InputValidationError(PuzzleToolError):
    code = "INPUT_VALIDATION_ERROR"


class SourceParseError(PuzzleToolError):
    code = "SOURCE_PARSE_ERROR"


class NoCandidateError(PuzzleToolError):
    code = "NO_VALID_CANDIDATE"


class CandidateValidationError(PuzzleToolError):
    code = "CANDIDATE_VALIDATION_ERROR"


class ProofLimitExceeded(PuzzleToolError):
    code = "PROOF_LIMIT_EXCEEDED"


class ExportConflictError(PuzzleToolError):
    code = "EXPORT_CONFLICT"


class ReconciliationRequiredError(PuzzleToolError):
    code = "RECONCILIATION_REQUIRED"


class OperationCancelledError(PuzzleToolError):
    """Raised only at a cooperative checkpoint; completed work stays valid."""

    code = "OPERATION_CANCELLED"
