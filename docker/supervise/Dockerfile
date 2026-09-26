# Builds the static musl supervisor. The base is pinned by digest so the bytes are reproducible.
FROM alpine:3.22@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1 AS build
RUN apk add --no-cache build-base=0.5-r3
WORKDIR /src
COPY Makefile supervise.h *.c ./
RUN make

FROM scratch
COPY --from=build /src/supervise /supervise
