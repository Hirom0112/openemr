"""Auth package — JWT verification + FHIR client.

Re-exports ``request_principal_var`` so downstream modules (audit logger,
dispatcher) can import from a stable location regardless of where the
ContextVar is defined.
"""

from auth.jwt_middleware import request_principal_var
from auth.scope import PATIENT_KEYED_TOOLS, check_patient_scope

__all__ = ["request_principal_var", "check_patient_scope", "PATIENT_KEYED_TOOLS"]
