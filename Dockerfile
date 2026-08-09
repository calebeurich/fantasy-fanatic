# Built and run remotely by Google Cloud Build/Cloud Run - never built locally as
# part of the normal workflow, see LOGIC.md's "Hosting platform evaluation" section.
FROM python:3.12-slim

# The Claude Agent SDK shells out to the `claude` CLI as its transport (see
# agent/agent.py) - needs Node.js. Checked the real current requirement rather than
# guessing: @anthropic-ai/claude-code needs Node 22+ as of mid-2026.
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# ANTHROPIC_API_KEY is never baked into the image - .env is gitignored so it isn't
# even present in what Cloud Build pulls from GitHub. It must be set as a real
# Cloud Run environment variable (bound to a Secret Manager secret) when the service
# is configured in the console - agent.py's load_dotenv() no-ops harmlessly if no
# .env file exists, and reads straight from the real environment either way.

# Cloud Run injects the actual port via $PORT at runtime - 8080 is just the default
# a local `docker run` would use.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn agent.api:app --host 0.0.0.0 --port ${PORT}"]
