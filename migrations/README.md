# Ledger migrations

The migrations themselves live in `src/proofpr/ledger/migrations/`, inside the
package, so they are found wherever the process starts and ship with the wheel.

Forward-only SQL, applied in filename order and recorded in a
`schema_migrations` table.

Naming: `0001_initial.sql`, `0002_add_....sql`.

There is no down migration. The ledger is append-only evidence; a rollback that
rewrites history would defeat the receipt chain. To undo a schema change, write a
new forward migration.

First migration lands in M1.
