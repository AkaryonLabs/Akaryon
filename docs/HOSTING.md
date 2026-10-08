# Akaryon on Render

The web app, interactive Void Speaker model, and Python API run together on one Render web service. OpenAI and GitHub OAuth secrets live only in Render environment variables. The repository contains no local chat history, databases, or private `.env` files.

## Initial access

This release supports one invited owner workspace. GitHub sign-in checks both the configured immutable GitHub user ID and a verified email. Other accounts cannot access the interface or API. Do not add more users until conversation ownership and isolation are implemented.

Register a GitHub OAuth application with homepage `https://akaryon.live` and callback `https://akaryon.live/auth/callback`. Only the `user:email` scope is requested; repository access is unnecessary. Set `AKARYON_GITHUB_CLIENT_ID`, `AKARYON_GITHUB_CLIENT_SECRET`, `AKARYON_INVITED_EMAIL`, and `AKARYON_INVITED_GITHUB_ID` in Render. The invited account must have that email verified on GitHub.

Sessions expire after eight hours. Sign out revokes the stored session. The browser receives only opaque HttpOnly, Secure, SameSite cookies; neither API keys nor OAuth access tokens enter browser storage. Updating the invited account settings invalidates sessions for the previous account. There is no public signup route.

## Deployment

The Blueprint `render.yaml` defines a free Python web service and a free PostgreSQL database in Ohio. **Render Free Postgres expires after 30 days.** This is a pilot configuration, not indefinite storage. Arrange a durable database plan and backups before the expiry date. Free web instances sleep when idle, so the first request can be slow. No paid plan is selected by the Blueprint.

1. Publish the repository, including the 3D GLB and bundled Three.js runtime.
2. Open `https://dashboard.render.com/blueprint/new?repo=https://github.com/AkaryonLabs/Akaryon`.
3. Fill each `sync: false` value using Render's private environment fields. `AKARYON_OPENAI_API_KEY` is the OpenAI API key; the app uses `gpt-4o-mini` for chat.
4. Apply the Blueprint. The start command validates settings, migrates the database, and listens on Render's assigned `PORT`. Health checks use `/healthz`; detailed health information requires sign-in.
5. Add `akaryon.live` to the web service's custom domains, then apply the exact DNS records shown by Render at the domain's DNS provider. Wait for domain verification and TLS issuance before testing sign-in. Never copy unrelated DNS records or nameserver settings.
6. Verify signed-out API requests return 401, the owner can sign in, the 3D model renders, a real chat succeeds, and history survives a restart. Auto-deploy is disabled until a release is explicitly deployed.

The Render native runtime may also be created directly with the same build command (`pip install -r requirements-render.txt`) and start command (`python -m akaryon.hosted_launcher`). Attach its PostgreSQL connection string as `DATABASE_URL`. The launcher selects psycopg automatically.

## Runtime limits

Use one web worker/instance: active task execution and event streams are in process. Conversations, saved notes, sessions, and tasks persist in PostgreSQL; active generations do not survive a restart. The cloud launcher disables command, desktop, browser-launch, and filesystem grants. The hosted service starts a fresh workspace; it does not mount the owner's computer or upload local chats.

OpenAI charges API usage separately. Application estimates are not a substitute for monitoring provider billing. Search and image generation use the separately configured models and require model access in the OpenAI project; only chat connectivity has been verified during this setup.

For local development, use `.venv/Scripts/python scripts/run_local_openai.py` from the source checkout. This explicitly reads the existing private `dist/.env`, binds to `127.0.0.1:8021`, and stores local history in `.build/akaryon-openai.db`. Default source settings remain local-only and mock-backed unless configured otherwise.

## Validation

Run `python -m pytest -q`. `tests/test_hosted_auth.py` covers anonymous access, host and Origin checks, OAuth state and PKCE, callback replay, email verification, immutable account matching, session expiry, and logout revocation. Validate `render.yaml` against `https://render.com/schema/render.yaml.json` before applying it.
