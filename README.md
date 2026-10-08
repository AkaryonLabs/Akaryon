# Akaryon

Akaryon is a conversational workspace with an interactive 3D Void Speaker, OpenAI-backed chat, saved conversation history, memory, and tools designed to require explicit permissions.

The Windows source release defaults to a local-only server and a mock model. See [HOSTING.md](docs/HOSTING.md) to deploy the invite-only single-owner web experience on Render.

Hosted access starts at `https://akaryon.live` after GitHub OAuth, an invited verified email, PostgreSQL, and the API keys are configured. The deployment Blueprint currently uses Render's free database, which expires after 30 days; choose durable storage before that deadline. OpenAI API usage is billed by OpenAI separately from Render hosting.

The 3D device source and rebuild instructions are in `workspace/void-speaker/` in the development checkout. Render receives the prebuilt model and locally bundled viewer; it does not require Node.js at runtime.
