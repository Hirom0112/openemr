import { Suspense } from "react";
import { redirect } from "next/navigation";
import { auth } from "@/auth";
import PatientHeader, {
  PatientHeaderSkeleton,
} from "@/components/cards/PatientHeader";

export default async function PatientPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const session = await auth();

  if (!session) {
    redirect(`/login?callbackUrl=/patient/${id}`);
  }

  return (
    <main className="flex flex-1 flex-col gap-3 p-4">
      <Suspense fallback={<PatientHeaderSkeleton />}>
        <PatientHeader patientId={id} />
      </Suspense>
      {/* TODO Phase 4.2-4.7: render the 6 cards in a two-column layout per inventory */}
    </main>
  );
}
