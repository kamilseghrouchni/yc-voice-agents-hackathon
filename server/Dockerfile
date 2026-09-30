FROM dailyco/pipecat-base:latest

# Enable bytecode compilation
ENV UV_COMPILE_BYTECODE=1

# Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy

# Uncomment if you wish to print a summary of the features available in the base image
# ENV PCC_LOG_FEATURES_SUMMARY=true

# Install the project's dependencies using the lockfile and settings
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Copy the application code.
# Pipecat Cloud expects an entrypoint at /app/bot.py — we ship bot-biobank.py
# as bot.py so the platform finds it.
COPY ./bot-biobank.py bot.py
COPY ./biobank_backend.py biobank_backend.py
COPY ./nvidia_stt.py nvidia_stt.py
COPY ./nemotron_llm.py nemotron_llm.py
COPY ./biobanks ./biobanks
COPY ./prompts ./prompts