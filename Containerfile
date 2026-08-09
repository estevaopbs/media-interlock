FROM docker.io/library/python@sha256:ff83a535339812dd72e69c93b3c48ddf7c85a324d6330af5797c82a255dbeef4 AS build

WORKDIR /build
ARG SOURCE_DATE_EPOCH=0
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}
COPY . .
RUN python -m pip install --no-cache-dir --require-hashes --no-deps --requirement build-requirements.txt \
 && python -m build --wheel --no-isolation --outdir /wheel

FROM docker.io/library/python@sha256:ff83a535339812dd72e69c93b3c48ddf7c85a324d6330af5797c82a255dbeef4 AS runtime

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin media-interlock
COPY --from=build /wheel/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl \
 && rm -f /tmp/*.whl
USER media-interlock

FROM runtime AS fence
ENTRYPOINT ["media-interlock-fence"]

FROM runtime AS publisher
ENTRYPOINT ["media-interlock-publisher"]
