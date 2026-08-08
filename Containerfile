FROM python:3.14.6-slim-bookworm AS build

WORKDIR /build
COPY . .
RUN python -m pip install --no-cache-dir build \
 && python -m build --wheel --outdir /wheel

FROM python:3.14.6-slim-bookworm AS runtime

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin media-interlock
COPY --from=build /wheel/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl \
 && rm -f /tmp/*.whl
USER media-interlock

FROM runtime AS fence
ENTRYPOINT ["media-interlock-fence"]

FROM runtime AS publisher
ENTRYPOINT ["media-interlock-publisher"]
