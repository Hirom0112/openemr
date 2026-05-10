/**
 * GET /api/documents/<uuid>
 *
 * Server-side proxy that streams a Co-Pilot source document (PDF / image /
 * text bytes) from OpenEMR's FHIR Binary endpoint to the dashboard browser.
 * The OpenEMR FHIR DocumentReference projection emits
 * `content[].attachment.url = "<base>/fhir/Binary/<uuid>"` (see
 * `src/Services/FHIR/DocumentReference/Trait/FhirDocumentReferenceTrait.php`),
 * and the same OAuth bearer the cards already use is accepted there.
 *
 * We proxy rather than redirect so the bearer token never lands in the
 * browser. The dashboard's existing Auth.js session is the trust boundary.
 */
import { NextResponse, type NextRequest } from "next/server";

import { authSessionTokenProvider } from "@/lib/fhir/token";

const UUID_RE = /^[0-9a-fA-F-]{32,36}$/;

export async function GET(
  _req: NextRequest,
  ctx: { params: Promise<{ uuid: string }> },
): Promise<Response> {
  const { uuid } = await ctx.params;
  if (!UUID_RE.test(uuid)) {
    return NextResponse.json({ error: "bad_uuid" }, { status: 400 });
  }

  const baseUrl = process.env.OPENEMR_BASE_URL;
  if (!baseUrl) {
    return NextResponse.json(
      { error: "server_misconfigured" },
      { status: 500 },
    );
  }

  let token: string;
  try {
    token = await authSessionTokenProvider.getAccessToken();
  } catch {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const url = `${baseUrl.replace(/\/+$/, "")}/apis/default/fhir/Binary/${encodeURIComponent(uuid)}`;
  let upstream: Response;
  try {
    upstream = await fetch(url, {
      headers: {
        Authorization: `Bearer ${token}`,
        // FHIR Binary returns the raw bytes when requested with the resource's
        // own contentType; */* is the safest ask. The OpenEMR endpoint honours
        // it and sets Content-Type on the way out.
        Accept: "*/*",
      },
      cache: "no-store",
    });
  } catch (cause) {
    console.error("[api/documents] upstream fetch failed", cause);
    return NextResponse.json({ error: "upstream_unreachable" }, { status: 502 });
  }

  if (!upstream.ok) {
    return NextResponse.json(
      { error: "upstream_error", status: upstream.status },
      { status: upstream.status === 404 ? 404 : 502 },
    );
  }

  const headers = new Headers();
  const ct = upstream.headers.get("content-type");
  if (ct) headers.set("Content-Type", ct);
  const cl = upstream.headers.get("content-length");
  if (cl) headers.set("Content-Length", cl);
  // Inline so iframes / <img> can render directly.
  headers.set("Content-Disposition", "inline");
  headers.set("Cache-Control", "private, max-age=60");
  // Do not let the browser sniff the bytes.
  headers.set("X-Content-Type-Options", "nosniff");

  return new Response(upstream.body, { status: 200, headers });
}
