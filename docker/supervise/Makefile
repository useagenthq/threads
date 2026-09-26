SRC = main.c state.c record.c proc.c run.c child.c terminate.c
# Reproducible: no build id, no compiler ident, no absolute paths in the object.
CFLAGS = -std=c11 -O2 -static -Wall -Wextra -Werror -pedantic -fno-ident \
         -ffile-prefix-map=$(CURDIR)=. -D_FILE_OFFSET_BITS=64
LDFLAGS = -static -Wl,--build-id=none

# Linux-only: see supervise.h. scripts/build-supervisor.sh compiles this in a container.
supervise: $(SRC) supervise.h
	@[ "$$(uname -s)" = Linux ] || { echo "build with scripts/build-supervisor.sh (Linux only)"; exit 1; }
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(SRC)
	strip --strip-all $@

clean:
	rm -f supervise

.PHONY: clean
