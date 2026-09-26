/* `supervise <key-hash> <deadline_ms> [--stdin] <argv…>`: one command, under the lock. */
#define _GNU_SOURCE
#include "supervise.h"

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/file.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

void child_main(const char *key, int with_stdin, char *const argv[], int lock_fd, int ack_fd,
                int go_fd);

#define GENERATION_WAIT_MS 10000
#define SWEEP_DEADLINE_S 5

static int wake[2];

static void on_signal(int sig) {
  char byte = (char)sig;
  ssize_t ignored = write(wake[1], &byte, 1);
  (void)ignored;
}

static long long now_ms(void) {
  struct timespec ts;
  clock_gettime(CLOCK_MONOTONIC, &ts);
  return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/* A stop request counts only when it names this key and this supervisor's (pid, start). */
static int stop_requested(const char *key, const struct record *mine) {
  char path[512];
  if (state_path(path, sizeof path, "stop", key) != 0) return 0;
  char body[256];
  if (read_small_file(path, body, sizeof body) <= 0) return 0;
  char want[256];
  int n = snprintf(want, sizeof want, "%s %ld %llu", key, mine->supervisor_pid,
                   mine->supervisor_start);
  if (n <= 0 || (size_t)n >= sizeof want) return 0;
  char *nl = strchr(body, '\n');
  if (nl != NULL) *nl = '\0';
  return strcmp(body, want) == 0;
}

static void clear_stop(const char *key) {
  char path[512];
  if (state_path(path, sizeof path, "stop", key) == 0) unlink(path);
}

static int take_lock(void) {
  int fd = open(STATE "/lock", O_RDWR | O_CREAT | O_CLOEXEC, 0600);
  if (fd < 0) die("cannot open the state lock");
  if (flock(fd, LOCK_EX) != 0) die("cannot take the state lock");
  return fd;
}

/* Waits for the child, a stop request or the deadline. Returns the wait status, or -1. */
static int await_child(pid_t child, const char *key, const struct record *mine,
                       long long deadline_ms) {
  long long ends_at = now_ms() + deadline_ms;
  for (;;) {
    int status = 0;
    pid_t seen = waitpid(child, &status, WNOHANG);
    if (seen == child) return status;
    if (seen < 0 && errno != EINTR) return -1;
    if (stop_requested(key, mine)) return -1;
    long long left = ends_at - now_ms();
    if (left <= 0) return -1;
    struct pollfd p = {.fd = wake[0], .events = POLLIN, .revents = 0};
    poll(&p, 1, left < 1000 ? (int)left : 1000);
    char drain[64];
    while (read(wake[0], drain, sizeof drain) > 0) continue;
  }
}

static int exit_code_of(int status) {
  if (WIFEXITED(status)) return WEXITSTATUS(status);
  if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
  return EX_ERROR;
}

static void finish(struct record *r, int status, int have_status) {
  int swept = sweep_command_processes(SWEEP_DEADLINE_S);
  /* The child the sweep killed is ours to reap; its status is not the command's own. */
  if (!have_status) waitpid((pid_t)r->child_pid, NULL, swept == 0 ? 0 : WNOHANG);
  if (swept != 0) {
    strcpy(r->state, "stuck");
  } else if (have_status) {
    strcpy(r->state, "exited");
    r->exit_code = exit_code_of(status);
  } else {
    strcpy(r->state, "terminated");
  }
  /* On ENOSPC the record stays `running`; D-2 then settles it with a container stop. */
  if (record_write(r) != 0) fprintf(stderr, "threads: the final record could not be written\n");
}

int mode_run(const char *key, long long deadline_ms, int with_stdin, char *const argv[]) {
  int lock_fd = take_lock();
  struct record mine;
  memset(&mine, 0, sizeof mine);
  snprintf(mine.key, sizeof mine.key, "%s", key);
  if (generation_now(mine.generation, sizeof mine.generation) != 0) die("no container generation");
  if (generation_await(mine.generation, GENERATION_WAIT_MS) != 0) die("the container is not ready");
  int live = records_any_live(mine.generation);
  if (live < 0) die("the records directory is unreadable");
  if (live > 0) {
    fprintf(stderr, "threads: admission refused\n");
    return EX_ADMISSION_REFUSED;
  }
  clear_stop(key);

  /* O_CLOEXEC on all three: the child uses the ack and go pipes before it execs, and no
     descriptor of the supervisor's own may survive into the command. */
  if (pipe2(wake, O_CLOEXEC) != 0) die("cannot open the wake pipe");
  int ack[2], go[2];
  if (pipe2(ack, O_CLOEXEC) != 0 || pipe2(go, O_CLOEXEC) != 0) die("cannot open the child pipes");
  fcntl(wake[0], F_SETFL, O_NONBLOCK);
  struct sigaction sa;
  memset(&sa, 0, sizeof sa);
  sa.sa_handler = on_signal;
  sa.sa_flags = SA_RESTART | SA_NOCLDSTOP;
  sigaction(SIGCHLD, &sa, NULL);
  sigaction(SIGUSR1, &sa, NULL);

  pid_t child = fork();
  if (child < 0) die("cannot fork the command");
  if (child == 0) {
    close(ack[0]);
    close(go[1]);
    child_main(key, with_stdin, argv, lock_fd, ack[1], go[0]);
    _exit(EX_ERROR);
  }
  close(ack[1]);
  close(go[0]);

  char ready = 0;
  if (read(ack[0], &ready, 1) != 1) {
    /* The child never dropped to uid 1000, so nothing ran and no record is written. */
    close(go[1]);
    sweep_command_processes(SWEEP_DEADLINE_S);
    waitpid(child, NULL, 0);
    die("the command process could not drop privileges");
  }
  mine.child_pid = (long)child;
  mine.supervisor_pid = (long)getpid();
  mine.supervisor_start = proc_start_time(getpid());
  mine.deadline_ms = deadline_ms;
  strcpy(mine.state, "running");
  if (record_write(&mine) != 0) {
    close(go[1]);
    sweep_command_processes(SWEEP_DEADLINE_S);
    waitpid(child, NULL, 0);
    die("the running record could not be written");
  }
  /* No command byte runs without a record. */
  if (write(go[1], "g", 1) != 1) die("cannot release the command");

  int status = await_child(child, key, &mine, deadline_ms);
  finish(&mine, status, status >= 0);
  clear_stop(key);
  close(lock_fd);
  return EX_OK;
}
