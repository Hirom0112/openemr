import { signIn } from "@/auth";

export const dynamic = "force-dynamic";

export default function LoginPage({
  searchParams,
}: {
  searchParams?: Promise<{ callbackUrl?: string }>;
}) {
  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-6 p-8">
      <h1 className="text-2xl font-semibold">Sign in</h1>
      <p className="text-sm text-zinc-600">
        Authenticate against your OpenEMR instance to view patient data.
      </p>
      <form
        action={async () => {
          "use server";
          const params = (await searchParams) ?? {};
          await signIn("openemr", {
            redirectTo: params.callbackUrl ?? "/patient/4",
          });
        }}
      >
        <button
          type="submit"
          className="rounded-full bg-foreground px-6 py-3 text-sm font-medium text-background transition-colors hover:bg-[#383838] dark:hover:bg-[#ccc]"
        >
          Sign in with OpenEMR
        </button>
      </form>
    </main>
  );
}
