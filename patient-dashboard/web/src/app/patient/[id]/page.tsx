export default async function PatientPage({
    params,
}: {
    params: Promise<{ id: string }>;
}) {
    const { id } = await params;
    return <h1>Patient {id}</h1>;
}
