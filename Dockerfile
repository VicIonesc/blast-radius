# Same base image the organizers labeled every commit in (linux/amd64).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot source at the pinned commit (an allowed download), used to read the
# pre-change code each patch applies to.
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
ADD https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /opt/sqlglot \
 && tar -xzf /tmp/sqlglot.tar.gz -C /opt/sqlglot --strip-components=1 \
 && rm /tmp/sqlglot.tar.gz \
 && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests || true \
 && chmod -R a+rX /opt/sqlglot

WORKDIR /app
COPY blast/ /app/blast/
COPY map/ /app/map/
RUN python -m compileall -q /app/blast && chmod -R a+rX /app

ENV SQLGLOT_DIR=/opt/sqlglot \
    MAP_DIR=/app/map \
    DATA_DIR=/data \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python", "-m", "blast.select"]
