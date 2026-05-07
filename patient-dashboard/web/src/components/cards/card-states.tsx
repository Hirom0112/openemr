/**
 * Shared card-state primitives reused by every Phase 4 dashboard card.
 *
 * The pattern is: each card is a Server Component that fetches FHIR data
 * server-side. While that fetch is pending, the page wraps the card in
 * `<Suspense>` and renders `<LoadingCard />`. If the fetch throws, the card
 * catches and renders `<ErrorCard />`. If the fetch resolves to no clinical
 * data, the card renders `<EmptyCard />` with a card-specific message.
 *
 * These primitives are intentionally dumb — no card-specific logic lives
 * here. Cards that need different chrome (no title, custom skeleton shape)
 * compose their own at call-site rather than parameterising these any
 * further. See PatientHeader.tsx for the headless variant of LoadingCard.
 */
import * as React from "react";
import { TriangleAlert } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

interface CardStateProps {
  title: string;
}

/**
 * Loading skeleton for a sub-card (Allergies, Medications, etc.). Renders
 * a CardHeader with the card's title (so the user has affordance about
 * what is loading) and a stack of skeleton rows in the body.
 */
export function LoadingCard({ title }: CardStateProps): React.ReactElement {
  return (
    <Card size="sm" data-testid="loading-card" aria-busy="true">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <Skeleton className="h-3 w-3/4" />
        <Skeleton className="h-3 w-1/2" />
        <Skeleton className="h-3 w-2/3" />
      </CardContent>
    </Card>
  );
}

interface ErrorCardProps extends CardStateProps {
  message?: string;
}

/**
 * Error state for a sub-card. Caller passes a generic, non-leaky message;
 * the underlying exception should already be logged at the boundary that
 * throws. We deliberately do not surface `error.message` here — exception
 * messages may carry internal detail (URLs, tokens).
 */
export function ErrorCard({
  title,
  message = "Could not load this section. Try again later.",
}: ErrorCardProps): React.ReactElement {
  return (
    <Card size="sm" data-testid="error-card" role="alert">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="flex items-center gap-2 text-muted-foreground">
        <TriangleAlert className="h-4 w-4 shrink-0" aria-hidden="true" />
        <span>{message}</span>
      </CardContent>
    </Card>
  );
}

interface EmptyCardProps extends CardStateProps {
  message?: string;
}

/**
 * Empty state for a sub-card. The OpenEMR original distinguishes "Nothing
 * Recorded" (untouched) from "None"/"No Known Allergies" (touched), but
 * FHIR has no flag for that distinction (see dashboard-api-map.md, Q7).
 * Cards collapse to a single empty message; callers pass the wording that
 * matches the original card.
 */
export function EmptyCard({
  title,
  message = "Nothing recorded.",
}: EmptyCardProps): React.ReactElement {
  return (
    <Card size="sm" data-testid="empty-card">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="text-muted-foreground">{message}</CardContent>
    </Card>
  );
}
