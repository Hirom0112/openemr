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
                    {/*
                        Layout (corrected 2026-05-08, see
                        `parity-investigation-2026-05-08.md` § B3):

                          Row 1: Allergies | Medical Problems | Medications (3-up)
                          Row 2: Prescriptions full-width
                          Row 3: Care Team full-width
                          Row 4: Vitals full-width

                        This is "spirit of the layout" — the original
                        `demographics.php` places these cards across three
                        rows of a 4-up + col-md-8/col-md-4 grid that also
                        holds out-of-scope sections (Demographics, Insurance,
                        Labs, Portal, Reminders, etc.). A strict mirror would
                        require a `col-md-8` row whose right column is empty
                        for our scope; that degrades parity rather than
                        improves it. Defense paragraph in
                        `PATIENT_DASHBOARD_MIGRATION.md`.
                    */}
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                        <Suspense fallback={<AllergiesCardSkeleton />}>
                            <AllergiesCard patientUuid={patientUuid} />
                        </Suspense>
                        <Suspense fallback={<MedicalProblemsCardSkeleton />}>
                            <MedicalProblemsCard patientUuid={patientUuid} />
                        </Suspense>
                        <Suspense fallback={<MedicationsCardSkeleton />}>
                            <MedicationsCard patientUuid={patientUuid} />
                        </Suspense>
                    </div>

                    <Suspense fallback={<PrescriptionsCardSkeleton />}>
                        <PrescriptionsCard patientUuid={patientUuid} />
                    </Suspense>

                    <Suspense fallback={<CareTeamCardSkeleton />}>
                        <CareTeamCard patientUuid={patientUuid} />
                    </Suspense>

                    <Suspense fallback={<VitalsCardSkeleton />}>
                        <VitalsCard patientUuid={patientUuid} />
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
