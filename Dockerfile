# Built and run remotely by Google Cloud Build/Cloud Run - never built locally as
# part of the normal workflow, see LOGIC.md's "Hosting platform evaluation" section.
# Python only: the agent is a Messages API tool loop in this process (agent/tool_loop.py), so
# the image no longer carries Node.js or the `claude` CLI (~500MB of the old image).
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
# --retries/--timeout: the first Cloud Build attempt died on a corrupted wheel
# download ("PACKAGES DO NOT MATCH THE HASHES", unknown package) part-way through
# installing a fairly heavy dependency set. pip's defaults give up quickly on a
# flaky transfer; this makes the build tolerate one rather than failing outright.
RUN pip install --no-cache-dir --retries 5 --timeout 60 -r requirements.txt
COPY . .

# ANTHROPIC_API_KEY and DATABASE_URL are never baked into the image - .env is gitignored
# so it isn't even present in what Cloud Build pulls from GitHub. They are Cloud Run
# environment variables bound to Secret Manager secrets (see the deploy workflows);
# agent.py's load_dotenv() no-ops harmlessly if no .env file exists.

# Run as non-root - better container practice. HOME must be a real writable directory
# (libraries keep caches and config under it, and Cloud Run's filesystem is in-memory tmpfs).
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser
ENV HOME=/home/appuser

# Cloud Run injects the actual port via $PORT at runtime - 8080 is just the default
# a local `docker run` would use.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn agent.api:app --host 0.0.0.0 --port ${PORT}"]
