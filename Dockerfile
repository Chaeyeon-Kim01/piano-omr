# The recognition service, with homr's models fetched at build time so the
# first request does not wait for a download.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY piano_omr ./piano_omr
RUN pip install --no-cache-dir . && homr --init

# Listen beyond the container. Set PIANO_OMR_ORIGINS to the app's address and
# PIANO_OMR_SOURCE_URL to a public copy of this repository when running it.
ENV PIANO_OMR_HOST=0.0.0.0 \
    PIANO_OMR_PORT=8765
EXPOSE 8765
CMD ["piano-omr"]
