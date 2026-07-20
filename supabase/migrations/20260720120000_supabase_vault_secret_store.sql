-- Encrypted, server-only provider credentials. Workflow tables retain only
-- opaque secret:// references; secret values live in Supabase Vault.

create extension if not exists supabase_vault with schema vault;
