"""Opened local projects for the loopback Runtime API (P2.2).

Loading and validating a project is done by ``pulse_schema``; this registry only keeps the
resolved root and loaded documents so later requests cannot swap a path out from under a run.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pulse_runtime.api.contracts import (
    IssueModel,
    PreflightRequest,
    PreflightResponse,
    ProjectDetail,
    ProjectSummary,
)
from pulse_runtime.api.errors import ApiRefusal
from pulse_schema.validation import ContractError, Issue, Project, load_project, preflight


@dataclass(frozen=True)
class RegisteredProject:
    root: Path
    project: Project


def _issues(items: tuple[Issue, ...] | list[Issue]) -> list[IssueModel]:
    return [IssueModel(code=item.code, path=item.path, message=item.message) for item in items]


def _summary(registered: RegisteredProject) -> ProjectSummary:
    manifest = registered.project.manifest
    return ProjectSummary(
        root=str(registered.root),
        project_id=manifest.project_id,
        name=manifest.name,
        revision=manifest.revision,
        entry_cases=list(manifest.entry_cases),
        case_ids=sorted(registered.project.cases),
        flow_ids=sorted(registered.project.flows),
        data_ids=sorted(registered.project.data),
        profile_ids=sorted(registered.project.profiles),
        platforms=[str(item) for item in manifest.platforms],
        default_strategy=manifest.default_strategy,
        required_capabilities=list(manifest.required_capabilities),
        allowed_apps=list(manifest.constraints.allowed_apps),
    )


def _detail(registered: RegisteredProject) -> ProjectDetail:
    manifest = registered.project.manifest.model_dump(mode="json", by_alias=True)
    return ProjectDetail(**_summary(registered).model_dump(), manifest=manifest)


class ProjectRegistry:
    def __init__(self) -> None:
        self._projects: dict[str, RegisteredProject] = {}
        self._lock = threading.RLock()

    def open(self, path: str) -> ProjectDetail:
        candidate = Path(path).expanduser()
        try:
            project = load_project(candidate)
        except ContractError as exc:
            raise ApiRefusal(
                "project_invalid", 422, issues=[issue.model_dump() for issue in _issues(exc.issues)]
            ) from None
        except OSError:
            raise ApiRefusal("project_unreadable", 404) from None
        root = candidate.resolve()
        registered = RegisteredProject(root, project)
        with self._lock:
            existing = self._projects.get(project.manifest.project_id)
            if existing is not None and existing.root != root:
                raise ApiRefusal(
                    "project_id_conflict",
                    409,
                    project_id=project.manifest.project_id,
                    root=str(existing.root),
                )
            self._projects[project.manifest.project_id] = registered
        return _detail(registered)

    def get(self, project_id: str) -> RegisteredProject:
        with self._lock:
            registered = self._projects.get(project_id)
        if registered is None:
            raise ApiRefusal("project_not_open", 404, project_id=project_id)
        return registered

    def list(self) -> list[ProjectSummary]:
        with self._lock:
            values = list(self._projects.values())
        return [
            _summary(item)
            for item in sorted(values, key=lambda item: item.project.manifest.project_id)
        ]

    def detail(self, project_id: str) -> ProjectDetail:
        return _detail(self.get(project_id))

    def close(self, project_id: str) -> bool:
        with self._lock:
            return self._projects.pop(project_id, None) is not None

    def case(self, project_id: str, case_id: str) -> dict[str, Any]:
        project = self.get(project_id).project
        case = project.cases.get(case_id)
        if case is None:
            raise ApiRefusal("case_not_found", 404, case_id=case_id)
        return case.model_dump(mode="json", by_alias=True)

    def cases(self, project_id: str) -> dict[str, Any]:
        project = self.get(project_id).project
        return {
            case_id: case.model_dump(mode="json", by_alias=True)
            for case_id, case in sorted(project.cases.items())
        }

    def profile(self, project_id: str, profile_id: str) -> dict[str, Any]:
        project = self.get(project_id).project
        profile = project.profiles.get(profile_id)
        if profile is None:
            raise ApiRefusal("profile_not_found", 404, profile_id=profile_id)
        return profile.model_dump(mode="json", by_alias=True)

    def flow(self, project_id: str, flow_id: str) -> dict[str, Any]:
        project = self.get(project_id).project
        flow = project.flows.get(flow_id)
        if flow is None:
            raise ApiRefusal("flow_not_found", 404, flow_id=flow_id)
        return flow.model_dump(mode="json", by_alias=True)

    def data(self, project_id: str, data_id: str) -> dict[str, Any]:
        project = self.get(project_id).project
        data = project.data.get(data_id)
        if data is None:
            raise ApiRefusal("data_not_found", 404, data_id=data_id)
        return data.model_dump(mode="json", by_alias=True)

    def preflight(self, project_id: str, request: PreflightRequest) -> PreflightResponse:
        project = self.get(project_id).project
        case = project.cases.get(request.case_id)
        if case is None:
            raise ApiRefusal("case_not_found", 404, case_id=request.case_id)
        profile_id = request.profile_id or (case.profile_refs[0] if case.profile_refs else None)
        if profile_id is None:
            raise ApiRefusal("unknown_profile", 422, case_id=request.case_id)
        if profile_id not in case.profile_refs:
            raise ApiRefusal(
                "profile_not_declared", 422, case_id=request.case_id, profile_id=profile_id
            )
        strategy = request.strategy or project.manifest.default_strategy
        try:
            result = preflight(
                project,
                request.case_id,
                profile_id,
                strategy=strategy,
                inputs=request.inputs,
                model_available=request.model_available,
            )
        except ContractError as exc:
            raise ApiRefusal(
                "project_invalid", 422, issues=[issue.model_dump() for issue in _issues(exc.issues)]
            ) from None
        return PreflightResponse(
            offline_ready=result.offline_ready,
            issues=_issues(result.issues),
            ai_requirements=_issues(result.ai_requirements),
        )
