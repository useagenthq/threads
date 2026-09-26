/* `supervise --terminate <key-hash>`: the D-2 probe, run as uid 0 inside the container.
   It never blocks on the lock. Its exit status tells the host which D-2 row applies. */
#define _GNU_SOURCE
#include "supervise.h"

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <unistd.h>

/* Kills the container's main process. tini exits, and the kernel kills every process in the
   pid namespace, this probe included, so the lock is never handed to a waiting command. */
static int stop_container(void) {
  char body[64];
  if (read_small_file(STATE "/idle.pid", body, sizeof body) <= 0) {
    fprintf(stderr, "threads: no idle pid to stop\n");
    return EX_ERROR;
  }
  long pid = strtol(body, NULL, 10);
  if (pid <= 1) {
    fprintf(stderr, "threads: the idle pid is not usable\n");
    return EX_ERROR;
  }
  fprintf(stderr, "threads: stopping the container\n");
  if (kill((pid_t)pid, SIGKILL) != 0) return EX_ERROR;
  return EX_TERM_KILLED;
}

static int signal_supervisor(const char *key, const struct record *r) {
  char body[256];
  int n = snprintf(body, sizeof body, "%s %ld %llu\n", key, r->supervisor_pid,
                   r->supervisor_start);
  if (n <= 0 || (size_t)n >= sizeof body) return EX_ERROR;
  char path[512];
  if (state_path(path, sizeof path, "stop", key) != 0) return EX_ERROR;
  if (write_atomic(path, body, (size_t)n, 0600) != 0) return EX_ERROR;
  if (kill((pid_t)r->supervisor_pid, SIGUSR1) != 0) {
    return errno == ESRCH ? EX_TERM_GONE : EX_ERROR;
  }
  /* A pid can be reused, so the start time is re-checked after the signal. */
  if (proc_start_time((pid_t)r->supervisor_pid) != r->supervisor_start) return EX_TERM_GONE;
  return EX_TERM_SIGNALLED;
}

int mode_terminate(const char *key) {
  char generation[GEN_MAX];
  if (generation_now(generation, sizeof generation) != 0) die("no container generation");
  struct record r;
  if (record_read(key, &r) != 0 || !record_is_live(&r, generation)) return EX_OK;

  int lock_fd = open(STATE "/lock", O_RDWR | O_CREAT | O_CLOEXEC, 0600);
  if (lock_fd < 0) die("cannot open the state lock");
  if (flock(lock_fd, LOCK_EX | LOCK_NB) == 0) {
    /* Holding the lock proves K's supervisor is gone: only it could have held it. */
    if (record_read(key, &r) != 0 || !record_is_live(&r, generation)) {
      close(lock_fd);
      return EX_OK;
    }
    return stop_container();
  }
  if (errno != EWOULDBLOCK) die("cannot test the state lock");
  close(lock_fd);

  int live_supervisor = r.supervisor_pid > 0 &&
                        proc_start_time((pid_t)r.supervisor_pid) == r.supervisor_start;
  if (live_supervisor) return signal_supervisor(key, &r);
  /* The lock is held by another key's supervisor or by another probe; the host re-reads. */
  return r.supervisor_pid > 0 ? EX_TERM_GONE : EX_TERM_BUSY;
}
