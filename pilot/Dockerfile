ARG GO_IMAGE=golang:1.25.3-bookworm
ARG NODE_IMAGE=node:24.12.0-bookworm
ARG BACKEND_IMAGE=debian:bookworm-slim
ARG SERVICES_IMAGE=python:3.12.12-slim-bookworm
FROM ${GO_IMAGE} AS multica-build
ARG MULTICA_COMMIT
WORKDIR /upstream
RUN curl -fsSL --retry 3 "https://codeload.github.com/multica-ai/multica/tar.gz/${MULTICA_COMMIT}" | tar xz --strip-components=1
WORKDIR /upstream/server
ENV GOTOOLCHAIN=go1.26.9
COPY tools/q0_agent/probe.go /tmp/pilot-probe.go
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-X main.version=q0-pilot -X main.commit=${MULTICA_COMMIT}" -o /bin-out/multica-server ./cmd/server && \
    CGO_ENABLED=0 go build -trimpath -o /bin-out/migrate ./cmd/migrate && \
    CGO_ENABLED=0 go build -trimpath -o /bin-out/pilot-probe /tmp/pilot-probe.go

FROM ${BACKEND_IMAGE} AS multica
ARG MULTICA_COMMIT
LABEL q0.multica.commit=${MULTICA_COMMIT}
WORKDIR /app
COPY --from=multica-build /bin-out/multica-server /bin-out/migrate ./
COPY --from=multica-build /upstream/server/migrations ./migrations
COPY --from=multica-build /upstream/LICENSE /upstream/NOTICE ./
CMD ["/app/multica-server"]

FROM ${NODE_IMAGE} AS runtime-build
ARG DSH_COMMIT
ARG BRIDGE_COMMIT
LABEL q0.dsh.commit=${DSH_COMMIT} q0.bridge.commit=${BRIDGE_COMMIT}
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates && rm -rf /var/lib/apt/lists/*
RUN npm install -g pnpm@11.28.5 --registry=https://registry.npmjs.org
WORKDIR /dsh
RUN curl -fsSL --retry 3 "https://codeload.github.com/deepseek-ai/deepseek-harness/tar.gz/${DSH_COMMIT}" | tar xz --strip-components=1
# Mirror only the locked tarball. Its content must match the unmodified lockfile digest.
RUN mkdir -p /tmp/q0-package && \
    curl -fsSL --retry 3 --max-time 45 https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz -o /tmp/q0-package/xlsx.tgz && \
    test "$(openssl dgst -sha512 -binary /tmp/q0-package/xlsx.tgz | openssl base64 -A)" = 'oLDq3jw7AcLqKWH2AhCpVTZl8mf6X2YReP+Neh0SJUzV/BdZYjth94tG5toiMB1PPrYtxOCfaoUCkvtuH+3AJA==' && \
    sed -i 's|tarball: https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz|tarball: http://127.0.0.1:18637/xlsx.tgz|' pnpm-lock.yaml && \
    (python3 -m http.server 18637 --bind 127.0.0.1 --directory /tmp/q0-package >/tmp/q0-package.log 2>&1 & \
     mirror=$!; trap 'kill "$mirror"' EXIT; pnpm install --frozen-lockfile --ignore-scripts) && \
    sed -i 's|tarball: http://127.0.0.1:18637/xlsx.tgz|tarball: https://cdn.sheetjs.com/xlsx-0.20.3/xlsx-0.20.3.tgz|' pnpm-lock.yaml && \
    DSH_CLIENT_COMMIT_HASH="${DSH_COMMIT}" pnpm run build:artifacts
WORKDIR /bridge
RUN curl -fsSL --retry 3 "https://codeload.github.com/dsh-external/dsh-multica-runtime/tar.gz/${BRIDGE_COMMIT}" | tar xz --strip-components=1 && \
    npx --yes pnpm@10.34.5 install --frozen-lockfile --ignore-scripts && \
    npx --yes pnpm@10.34.5 run build
COPY --from=multica-build /bin-out/pilot-probe /usr/local/bin/pilot-probe

FROM runtime-build AS runtime
COPY tools/q0_agent/profile.patch.yml /pilot/profile.patch.yml
COPY pilot/runtime.py pilot/save_tool.py /pilot/
COPY --chmod=0755 pilot/dsh /pilot/dsh
ENTRYPOINT ["python3", "/pilot/runtime.py"]

FROM ${SERVICES_IMAGE} AS services
WORKDIR /app
COPY tools/q0_destination.py tools/q0_remote_compile.py tools/compile_fixture.py tools/verify_q0.py /app/
COPY pilot/services.py /app/
ENTRYPOINT ["python", "/app/services.py"]
