-- ============================================================================
-- Migration 001 — Developer-first pivot: REMOVE THE VAULT PRODUCT ENTIRELY
-- ----------------------------------------------------------------------------
-- What this platform was: password vault + daily tools with a code runner.
-- What it is now:        RunSpace — free code/bot hosting only.
--
-- This migration permanently DROPS the tables that backed:
--   Vault entries, Cards, Identities, Contacts, WiFi passwords, Servers (SSH),
--   Recovery phrases (seeds), Notes/Diary, Bookmarks, Tasks, and also the
--   `api_keys` and `notifications` tables which had no remaining producers
--   (dead features — nothing wrote to them anymore).
--
-- KEPT tables (unchanged): users, sessions, user_2fa, login_history,
--   user_preferences, jobs, snippets, activity_log, admin_audit_log,
--   abuse_reports.
--
-- FK safety check performed before authoring this file:
--   every dropped table's only outbound FK is  user_id → users(id).
--   No kept table references a dropped one, so plain DROPs are safe.
--   wifi_shares is dropped BEFORE user_wifi (child first).
--
-- The same DROPs run automatically (idempotently) from database.py:init_db()
-- on every startup, so applying this file by hand is OPTIONAL — it exists as
-- the tracked, human-readable record of a destructive, deliberate change.
--
-- REVERSIBILITY: the data in these tables is NOT recoverable after this runs.
--   Re-deploying the pre-pivot commit restores the code, not the data.
-- ============================================================================

BEGIN;

DROP TABLE IF EXISTS wifi_shares;
DROP TABLE IF EXISTS user_wifi;
DROP TABLE IF EXISTS vault_entries;
DROP TABLE IF EXISTS user_notes;
DROP TABLE IF EXISTS user_bookmarks;
DROP TABLE IF EXISTS user_categories;
DROP TABLE IF EXISTS user_cards;
DROP TABLE IF EXISTS user_tasks;
DROP TABLE IF EXISTS user_identities;
DROP TABLE IF EXISTS user_contacts;
DROP TABLE IF EXISTS user_servers;
DROP TABLE IF EXISTS user_recovery;
DROP TABLE IF EXISTS api_keys;
DROP TABLE IF EXISTS notifications;

COMMIT;
