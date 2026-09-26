/* threads sandbox supervisor: shared declarations. See plans/specs/lanes/16-sandbox-layer-v2.md D1. */
#ifndef THREADS_SUPERVISE_H
#define THREADS_SUPERVISE_H

/* Linux-only by design: the tree kill is setresuid(1000,1000,0) + kill(-1) + back to root, and
   /proc is the only way to verify it. macOS has neither call, so a host build would compile the
   privilege drop as an implicit declaration and silently not drop. Build with
   scripts/build-supervisor.sh, which compiles in an Alpine container for amd64 and arm64. */
#ifndef __linux__
#error "the threads supervisor is Linux-only: build it with scripts/build-supervisor.sh"
#endif

#include <stddef.h>
#include <sys/types.h>

#define ROOT "/run/threads"
#define STATE ROOT "/state"
#define WORKSPACE "/workspace"
#define CMD_UID 1000
#define CMD_GID 1000

/* Exit statuses. The host parses these strictly, so they are part of the contract. */
#define EX_OK 0
#define EX_ERROR 1
#define EX_ADMISSION_REFUSED 125 /* run: a live command already holds this container */
#define EX_TERM_KILLED 10        /* --terminate: took the lock, killed --idle; the container is stopping */
#define EX_TERM_SIGNALLED 11     /* --terminate: stop request written, SIGUSR1 sent to K's supervisor */
#define EX_TERM_GONE 12          /* --terminate: K's supervisor is gone; re-read K's record */
#define EX_TERM_BUSY 13          /* --terminate: the lock is held by someone other than K's supervisor */

/* Longest generation string: a 36-byte boot_id, a space and PID 1's start time. */
#define GEN_MAX 128
#define KEY_MAX 64

/* state.c */
int key_is_valid(const char *key);
int state_path(char *out, size_t n, const char *dir, const char *key);
/* Reads a whole file into `out` (NUL-terminated). Returns the length, or -1. */
ssize_t read_small_file(const char *path, char *out, size_t n);
/* Writes `len` bytes to `path` through a temp file, fsync and rename. Returns 0, or -1. */
int write_atomic(const char *path, const char *data, size_t len, mode_t mode);
/* The container generation: boot_id plus PID 1's start time. Returns 0, or -1. */
int generation_now(char *out, size_t n);
/* Waits (bounded) for state/generation and checks it against `want`. Returns 0, or -1. */
int generation_await(const char *want, int timeout_ms);
void die(const char *msg);

/* record.c */
struct record {
  char key[KEY_MAX + 1];
  char generation[GEN_MAX];
  char state[16];
  long child_pid;
  long supervisor_pid;
  unsigned long long supervisor_start;
  long long deadline_ms;
  int exit_code;
};
int record_write(const struct record *r);
/* Reads records/<key>.json. Returns 0 on success, -1 if absent or unreadable. */
int record_read(const char *key, struct record *out);
/* True when the record names a command that may still have live processes. */
int record_is_live(const struct record *r, const char *generation);
/* True if any current-generation record is live. Returns -1 if the directory can't be read. */
int records_any_live(const char *generation);

/* proc.c */
unsigned long long proc_start_time(pid_t pid);
/* Kills every CMD_UID process in one kernel sweep, then verifies. Returns 0, or -1 if any survive. */
int sweep_command_processes(int deadline_s);

/* modes */
int mode_idle(void);
int mode_check(void);
int mode_terminate(const char *key);
int mode_run(const char *key, long long deadline_ms, int with_stdin, char *const argv[]);

#endif
