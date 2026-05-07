import { redirect } from "next/navigation";
import { auth, signOut } from "@/auth";

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
    <main className="flex flex-1 flex-col gap-4 p-8">
      <h1 className="text-2xl font-semibold">Patient {id}</h1>
      <p>
        Signed in as{" "}
        <strong>
          {session.user?.email ?? session.user?.name ?? "unknown"}
        </strong>
      </p>
      <p>
        fhirUser: <code>{session.fhirUser ?? "n/a"}</code>
      </p>
      {session.error ? (
        <p className="text-red-600">
          Session error: {session.error} — please sign in again.
        </p>
      ) : null}
      <form
        action={async () => {
          "use server";
          await signOut({ redirectTo: "/" });
        }}
      >
        <button type="submit" className="text-sm underline">
          Sign out
        </button>
      </form>
    </main>
  );
}
