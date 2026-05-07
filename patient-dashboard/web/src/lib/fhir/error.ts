/**
 * FhirApiError — error subclass thrown by FhirClient on non-2xx responses
 * or transport failures. Cards catch this at their render boundary to
 * decide between "re-auth needed" (status 401), "transient — retry" (5xx),
 * and "show generic error".
 */
export class FhirApiError extends Error {
  public readonly status: number;
  public readonly fhirOperationOutcome?: unknown;
  public override readonly cause?: unknown;

  constructor(
    message: string,
    options: {
      status: number;
      fhirOperationOutcome?: unknown;
      cause?: unknown;
    },
  ) {
    super(message);
    this.name = "FhirApiError";
    this.status = options.status;
    this.fhirOperationOutcome = options.fhirOperationOutcome;
    this.cause = options.cause;
  }
}
