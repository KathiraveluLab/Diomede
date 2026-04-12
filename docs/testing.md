# Testing

## Prerequisites
- The test suite primarily uses mocked or isolated database interactions.
- For most tests, a real database instance is **not** required.
- Database access is typically bypassed with fixtures/patches so tests can run against controlled in-memory behavior instead of live persistence.

## Troubleshooting
If tests fail during import because of DB connection or ping calls:
- Verify test-related environment variables are set correctly (for example, values that disable production DB startup).
- Disable or mock DB initialization in test runs so startup code does not attempt a live connection.
- Use common workarounds such as test-specific config, monkeypatching the DB client/init function, or switching DB setup to lazy initialization.
