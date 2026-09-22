# syntax=docker/dockerfile:1.7
FROM registry.jetbrains.team/p/junie-cli/containers/demo-base:2

COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN uv python install 3.14
