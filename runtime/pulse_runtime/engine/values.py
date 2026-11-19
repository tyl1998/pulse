"""Typed case values and lexical scopes.

Parameter, variable, data and secret namespaces stay explicit. There is no interpolation,
expression evaluation, implicit coercion or mutation of project data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pulse_runtime.engine.errors import EngineStop
from pulse_schema import models as m
from pulse_schema.validation import Project, compatible, scalar_type


@dataclass(frozen=True)
class TypedValue:
    value: m.Scalar
    type: m.ScalarType


class Scope:
    """Single-assignment bindings inside one lexical block, with read-only outer access."""

    def __init__(
        self,
        parent: Scope | None = None,
        values: Mapping[str, TypedValue] | None = None,
        *,
        parameters: Mapping[str, TypedValue] | None = None,
    ) -> None:
        self.parent = parent
        self.values: dict[str, TypedValue] = dict(values or {})
        self.parameters: dict[str, TypedValue] = (
            parent.parameters if parent is not None else dict(parameters or {})
        )

    def child(self, values: Mapping[str, TypedValue] | None = None) -> Scope:
        return Scope(self, values)

    def define(self, name: str, value: TypedValue) -> None:
        if name in self.values:
            raise EngineStop("blocked", "variable_redefinition")
        self.values[name] = value

    def lookup(self, name: str) -> TypedValue:
        scope: Scope | None = self
        while scope is not None:
            if name in scope.values:
                return scope.values[name]
            scope = scope.parent
        raise EngineStop("blocked", "unresolved_reference")


def typed(value: m.Scalar) -> TypedValue:
    return TypedValue(value, scalar_type(value))


def checked(actual: TypedValue, expected: m.ScalarType) -> TypedValue:
    if not compatible(actual.type, expected):
        raise EngineStop("blocked", "value_type_mismatch")
    return actual


def resolve_value(
    value: m.Value,
    *,
    project: Project,
    scope: Scope,
    secrets: Mapping[str, str] | None,
) -> TypedValue:
    if isinstance(value, m.ParameterRef):
        if value.param_ref not in scope.parameters:
            raise EngineStop("blocked", "unresolved_reference")
        return scope.parameters[value.param_ref]
    if isinstance(value, m.VariableRef):
        return scope.lookup(value.var_ref)
    if isinstance(value, m.DataRef):
        data = project.data.get(value.data_ref)
        if data is None or value.key not in data.values:
            raise EngineStop("blocked", "unresolved_reference")
        item = data.values[value.key]
        if isinstance(item, m.SecretRef):
            return _secret(item.secret_ref, secrets)
        return typed(item)
    if isinstance(value, m.SecretRef):
        return _secret(value.secret_ref, secrets)
    return typed(value)


def _secret(name: str, secrets: Mapping[str, str] | None) -> TypedValue:
    value = (secrets or {}).get(name)
    if value is None:
        raise EngineStop("blocked", "secret_unavailable")
    return TypedValue(value, "string")


def seed_scope(
    case: m.Case,
    *,
    project: Project,
    inputs: Mapping[str, m.Scalar] | None,
    secrets: Mapping[str, str] | None,
) -> Scope:
    values: dict[str, TypedValue] = {}
    supplied = inputs or {}
    for name, parameter in case.parameters.items():
        if name in supplied:
            values[name] = checked(typed(supplied[name]), parameter.type)
        elif name in case.inputs:
            values[name] = checked(
                resolve_value(case.inputs[name], project=project, scope=Scope(), secrets=secrets),
                parameter.type,
            )
        elif "default" in parameter.model_fields_set:
            default = parameter.default
            if default is None:
                raise EngineStop("blocked", "parameter_default")
            values[name] = checked(typed(default), parameter.type)
        else:
            raise EngineStop("blocked", "input_required")
    return Scope(parameters=values)


def flow_scope(
    flow: m.Flow,
    arguments: Mapping[str, TypedValue],
) -> Scope:
    values: dict[str, TypedValue] = {}
    for name, parameter in flow.parameters.items():
        if name in arguments:
            values[name] = checked(arguments[name], parameter.type)
        elif "default" in parameter.model_fields_set:
            default = parameter.default
            if default is None:
                raise EngineStop("blocked", "parameter_default")
            values[name] = checked(typed(default), parameter.type)
        else:
            raise EngineStop("blocked", "flow_argument_missing")
    return Scope(parameters=values)
