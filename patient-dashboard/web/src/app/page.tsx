import { redirect } from "next/navigation";

/**
 * Root entry — there is no standalone landing page in this port.
 * Send the user straight to Gloria Tran (pid 4), the default
 * verification patient. From there auth gating takes over: an
 * unauthenticated user bounces to `/login?callbackUrl=/patient/4`,
 * an authenticated one sees the dashboard.
 */
export default function Home(): never {
    redirect("/patient/4");
}
