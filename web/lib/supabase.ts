import { createClient, type SupabaseClient } from "@supabase/supabase-js";

let browserClient: SupabaseClient | undefined;

function configured(value: string | undefined): value is string {
  return Boolean(value && !value.includes("replace-with"));
}

/**
 * Create the browser-only Supabase Auth client.
 *
 * This intentionally accepts only the project URL and publishable key. The
 * browser never receives the Postgres URL, a service-role key, or provider
 * credentials; all AccountingOS data access goes through FastAPI.
 */
export function supabaseBrowserClient(): SupabaseClient | null {
  const projectUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const publishableKey = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;
  if (!configured(projectUrl) || !configured(publishableKey)) {
    return null;
  }
  browserClient ??= createClient(projectUrl, publishableKey, {
    auth: {
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: true,
    },
  });
  return browserClient;
}

export function supabaseBrowserIsConfigured(): boolean {
  return supabaseBrowserClient() !== null;
}
