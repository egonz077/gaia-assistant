# Known issues carried out of increment 1

Deferred or parked during the rebuild, each with the reasoning recorded at the time.

Task 1-2: minor (deferred): get_pool() assigns the _pool singleton without a lock.
Task 1-2: minor (deferred): pyproject.toml has no [build-system] table. PICK UP AT
Task 3: minor (deferred): IS DISTINCT FROM guard in the trigger is defensive dead code
Task 4: minor (deferred): list-users e2e test asserts on stdout only, not cross-checked
Task 5: parked — get_or_create TOCTOU race — Ruling: spec deliberately rejects a unique
Task 5: minor (deferred): merge_profile's two UPDATEs are not wrapped in an explicit
Task 6: minor (deferred): test_save_creates_contacts_and_commitments asserts only on
Task 6: minor (deferred): meetings.recent() has no test AND no consumer anywhere in the
Task 7: DEFERRED FINDING for the final review — memory.search() returns ct.name via a
Task 8: minor (deferred): the offender-injection check that proves the signature
Task 11: minor (deferred): download_media does not check the metadata GET status before
KNOWN LIMITATION recorded for the final review — Task 10's tests run entirely against
Task 14: minor (deferred): tool schemas type ID fields as plain `string` with no format
OPEN QUESTION, unresolved and recorded honestly: rows I personally watched commit during
