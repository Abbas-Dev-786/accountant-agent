-- Corrective hardening for the Vault extension migration. This must remain a
-- separate migration because the extension may already be installed remotely.

revoke all on schema vault from public, anon, authenticated;
revoke all on all tables in schema vault from public, anon, authenticated;
revoke all on all sequences in schema vault from public, anon, authenticated;

-- The Vault extension owns internal cryptographic helper functions (for
-- example `_crypto_aead_det_noncegen`).  The Supabase migration role may not
-- alter those functions' ACLs, so a bulk REVOKE over every function fails on
-- a linked project.  Revoking USAGE on the vault schema already prevents
-- anon/authenticated from resolving or calling any Vault function, while the
-- table/view revocation above protects decrypted_secrets directly. Default
-- privileges are intentionally not changed: they would apply only to objects
-- created by the migration role, not to future extension-owned Vault objects.
