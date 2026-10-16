# Agent Session Bridge

- CLI and Python package: `src/agent_session_bridge/`.
- Reuse the pinned `session-migrate` event model and serializers. Keep DSH's codec local.
- Never read real user conversations for fixtures. Use synthetic sessions.
- Never mutate a source session, copy credentials, overwrite a destination, or write a live store during tests.
- Migration previews must stay content-free. Count dropped data in the manifest.
- An exported file is not proof of native resume. Label schema, native-read and model-continuation checks separately.
- Native schema changes need upstream source evidence in `docs/compatibility.md`.
- Verify: `python -m pytest` and `python -m ruff check .`.
