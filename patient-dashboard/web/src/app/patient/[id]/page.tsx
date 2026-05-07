import { Suspense } from "react";
import { redirect } from "next/navigation";
import { auth } from "@/auth";
import PatientHeader, {
    PatientHeaderSkeleton,
} from "@/components/cards/PatientHeader";
import AllergiesCard, {
    AllergiesCardSkeleton,
} from "@/components/cards/AllergiesCard";
import MedicalProblemsCard, {
    MedicalProblemsCardSkeleton,
} from "@/components/cards/MedicalProblemsCard";
import MedicationsCard, {
    MedicationsCardSkeleton,
} from "@/components/cards/MedicationsCard";
import PrescriptionsCard, {
    PrescriptionsCardSkeleton,
} from "@/components/cards/PrescriptionsCard";
import CareTeamCard, {
    CareTeamCardSkeleton,
} from "@/components/cards/CareTeamCard";
import VitalsCard, {
    VitalsCardSkeleton,
} from "@/components/cards/VitalsCard";
import { ErrorCard } from "@/components/cards/card-states";
import { FhirClient } from "@/lib/fhir/client";
import { authSessionTokenProvider } from "@/lib/fhir/token";

async function resolvePatientUuid(pid: string): Promise<string | null> {
    const baseUrl = process.env.OPENEMR_BASE_URL;
    if (!baseUrl) return null;
    // Match the suffix every card applies — `OPENEMR_BASE_URL` is the
    // OpenEMR origin, the FHIR API hangs off `/apis/default/fhir`.
    // (Follow-up: hoist this into a shared helper or have FhirClient
    // know the FHIR path internally so callers don't repeat themselves.)
    const client = new FhirClient({
        baseUrl: `${baseUrl.replace(/\/+$/, "")}/apis/default/fhir`,
        tokenProvider: authSessionTokenProvider,
    });
    const patient = await client.getPatientByPid(pid);
    return patient?.id ?? null;
}

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

    let patientUuid: string | null = null;
    let resolveError: string | null = null;
    try {
        patientUuid = await resolvePatientUuid(id);
    } catch (err) {
        resolveError = err instanceof Error ? err.message : "Unknown error";
    }

    return (
        <main className="flex flex-1 flex-col gap-3 p-4">
            <Suspense fallback={<PatientHeaderSkeleton />}>
                <PatientHeader patientId={id} />
            </Suspense>

            {patientUuid ? (
                <>
                    {/* Two-column body: left = inline clinical cards, right = vitals.
                        Mirrors dashboard-inventory.md per-card "## Position" rules:
                        left column holds Allergies / Medical Problems / Medications /
                        Prescriptions in a 2x2 grid; right column holds Vitals.
                        Care Team is full-width below (per inventory ## Care Team
                        ### Position — col-12 in original demographics.php:1268). */}
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                        <div className="grid grid-cols-1 gap-3 md:col-span-2 md:grid-cols-2">
                            <Suspense fallback={<AllergiesCardSkeleton />}>
                                <AllergiesCard patientUuid={patientUuid} />
                            </Suspense>
                            <Suspense
                                fallback={<MedicalProblemsCardSkeleton />}
                            >
                                <MedicalProblemsCard
                                    patientUuid={patientUuid}
                                />
                            </Suspense>
                            <Suspense fallback={<MedicationsCardSkeleton />}>
                                <MedicationsCard patientUuid={patientUuid} />
                            </Suspense>
                            <Suspense
                                fallback={<PrescriptionsCardSkeleton />}
                            >
                                <PrescriptionsCard patientUuid={patientUuid} />
                            </Suspense>
                        </div>
                        <div className="md:col-span-1">
                            <Suspense fallback={<VitalsCardSkeleton />}>
                                <VitalsCard patientUuid={patientUuid} />
                            </Suspense>
                        </div>
                    </div>

                    <Suspense fallback={<CareTeamCardSkeleton />}>
                        <CareTeamCard patientUuid={patientUuid} />
                    </Suspense>
                </>
            ) : (
                <ErrorCard
                    title="Patient"
                    message={
                        resolveError ??
                        `Patient ${id} was not found in OpenEMR.`
                    }
                />
            )}
        </main>
    );
}
