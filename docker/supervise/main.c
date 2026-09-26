/* Entry point: --idle (the container's command), --check, --terminate, or one command run. */
#define _GNU_SOURCE
#include "supervise.h"

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define DEADLINE_CAP_MS 3600000

static const char *const REPORTED[] = {"sh", "env", "bash", "tar", "git", "python3"};

static int on_path(const char *tool) {
  static const char *const SEARCH[] = {"/usr/local/sbin", "/usr/local/bin", "/usr/sbin",
                                       "/usr/bin",       "/sbin",          "/bin"};
  for (size_t i = 0; i < sizeof SEARCH / sizeof SEARCH[0]; i++) {
    char path[512];
    if (snprintf(path, sizeof path, "%s/%s", SEARCH[i], tool) >= (int)sizeof path) continue;
    if (access(path, X_OK) == 0) return 1;
  }
  return 0;
}

int mode_check(void) {
  int present[sizeof REPORTED / sizeof REPORTED[0]];
  for (size_t i = 0; i < sizeof REPORTED / sizeof REPORTED[0]; i++) {
    present[i] = on_path(REPORTED[i]);
  }
  int have_sh = present[0] && access("/bin/sh", X_OK) == 0;
  present[0] = have_sh;
  printf("{");
  for (size_t i = 0; i < sizeof REPORTED / sizeof REPORTED[0]; i++) {
    printf("%s\"%s\":%s", i == 0 ? "" : ",", REPORTED[i], present[i] ? "true" : "false");
  }
  printf("}\n");
  return (have_sh && present[1]) ? EX_OK : EX_ERROR;
}

static void make_dir(const char *path) {
  if (mkdir(path, 0700) != 0 && errno != EEXIST) die("cannot make the state directory");
}

int mode_idle(void) {
  /* Root code holds CHOWN; the command runs as uid 1000 with an empty permitted set.
     A named volume mounts owned by root, and the daemon offers no other way to fix it. */
  if (chown(WORKSPACE, CMD_UID, CMD_GID) != 0 && errno != ENOENT) {
    die("cannot give /workspace to the command uid");
  }
  make_dir(STATE "/records");
  make_dir(STATE "/stop");
  make_dir(STATE "/stdin");
  char pid[32];
  int n = snprintf(pid, sizeof pid, "%ld\n", (long)getpid());
  if (n <= 0 || write_atomic(STATE "/idle.pid", pid, (size_t)n, 0600) != 0) {
    die("cannot record the idle pid");
  }
  char generation[GEN_MAX];
  if (generation_now(generation, sizeof generation) != 0) die("no container generation");
  size_t len = strlen(generation);
  /* Written once, before anything can be admitted: admission waits for this file. */
  if (write_atomic(STATE "/generation", generation, len, 0600) != 0) {
    die("cannot record the container generation");
  }
  for (;;) pause();
}

static long long parse_deadline(const char *text) {
  char *end = NULL;
  long long ms = strtoll(text, &end, 10);
  if (end == NULL || *end != '\0' || ms <= 0) die("deadline_ms must be a positive integer");
  return ms > DEADLINE_CAP_MS ? DEADLINE_CAP_MS : ms;
}

int main(int argc, char *argv[]) {
  if (argc >= 2 && strcmp(argv[1], "--idle") == 0) return mode_idle();
  if (argc == 2 && strcmp(argv[1], "--check") == 0) return mode_check();
  if (argc == 3 && strcmp(argv[1], "--terminate") == 0) {
    if (!key_is_valid(argv[2])) die("the key hash must be lowercase hex");
    return mode_terminate(argv[2]);
  }
  if (argc < 4) {
    fprintf(stderr, "usage: supervise <key-hash> <deadline_ms> [--stdin] <argv…>\n");
    return EX_ERROR;
  }
  if (!key_is_valid(argv[1])) die("the key hash must be lowercase hex");
  long long deadline_ms = parse_deadline(argv[2]);
  int first = 3;
  int with_stdin = 0;
  if (strcmp(argv[3], "--stdin") == 0) {
    with_stdin = 1;
    first = 4;
  }
  if (first >= argc) die("no command was given");
  return mode_run(argv[1], deadline_ms, with_stdin, &argv[first]);
}
