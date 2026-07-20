-- Corrective hardening for the Vault extension migration. This must remain a
-- separate migration because the extension may already be installed remotely.

revoke all on schema vault from public, anon, authenticated;
revoke all on all tables in schema vault from public, anon, authenticated;
revoke all on all sequences in schema vault from public, anon, authenticated;
revoke all on all functions in schema vault from public, anon, authenticated;

alter default privileges in schema vault
revoke all on tables from public, anon, authenticated;
alter default privileges in schema vault
revoke all on sequences from public, anon, authenticated;
alter default privileges in schema vault
revoke all on functions from public, anon, authenticated;
