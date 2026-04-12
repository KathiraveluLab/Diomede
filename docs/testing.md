# Testing

## Prerequisites
- The test suite primarily uses mocked or isolated database interactions.
- For most tests, a real database instance is **not** required.
- Database access is typically bypassed with fixtures/patches so tests can run against controlled in-memory behavior instead of live persistence.

## Troubleshooting
If tests fail during import because of database connection attempts:
- Verify test-related environment variables are set correctly (for example, values that disable production DB startup).
- Disable or mock DB initialization in test runs so startup code does not attempt a live connection.
- Use common workarounds such as test-specific config, monkeypatching the database initialization, or switching to lazy initialization.
