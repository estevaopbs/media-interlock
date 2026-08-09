FROM docker.io/library/python@sha256:b5998102f95c4b44edf1e7cb5cecbe1f49e0bf054f345c1db5b854e166e6e17a AS build

WORKDIR /build
ARG SOURCE_DATE_EPOCH=0
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}
COPY . .
RUN python -m pip install --no-cache-dir --require-hashes --no-deps --requirement build-requirements.txt \
 && python -m build --wheel --no-isolation --outdir /wheel

FROM docker.io/library/python@sha256:b5998102f95c4b44edf1e7cb5cecbe1f49e0bf054f345c1db5b854e166e6e17a AS runtime

ARG SOURCE_REVISION=unknown
ARG PACKAGE_VERSION=0.0.0
LABEL org.opencontainers.image.source="https://github.com/estevaopbs/media-interlock" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${PACKAGE_VERSION}" \
      org.opencontainers.image.licenses="MIT"
COPY --from=build /wheel/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl \
 && rm -f /tmp/*.whl
USER 65532:65532

FROM runtime AS reconciler
ENTRYPOINT ["media-interlock-reconciler"]

FROM runtime AS fence
ENTRYPOINT ["media-interlock-fence"]

FROM runtime AS publisher
ENTRYPOINT ["media-interlock-publisher"]
