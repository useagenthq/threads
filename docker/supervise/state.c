/* Paths, atomic writes and the container generation. */
#define _GNU_SOURCE
#include "supervise.h"

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

void die(const char *msg) {
  fprintf(stderr, "threads: %s\n", msg);
  exit(EX_ERROR);
}

/* A key hash names a file under a root-only directory, so it is a trust boundary. */
int key_is_valid(const char *key) {
  size_t n = strlen(key);
  if (n == 0 || n > KEY_MAX) return 0;
  for (size_t i = 0; i < n; i++) {
    char c = key[i];
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return 0;
  }
  return 1;
}

int state_path(char *out, size_t n, const char *dir, const char *key) {
  if (!key_is_valid(key)) return -1;
  int written = snprintf(out, n, "%s/%s/%s", STATE, dir, key);
  return (written > 0 && (size_t)written < n) ? 0 : -1;
}

ssize_t read_small_file(const char *path, char *out, size_t n) {
  int fd = open(path, O_RDONLY | O_CLOEXEC);
  if (fd < 0) return -1;
  size_t got = 0;
  while (got + 1 < n) {
    ssize_t r = read(fd, out + got, n - 1 - got);
    if (r < 0) {
      if (errno == EINTR) continue;
      close(fd);
      return -1;
    }
    if (r == 0) break;
    got += (size_t)r;
  }
  close(fd);
  out[got] = '\0';
  return (ssize_t)got;
}

static int write_all(int fd, const char *data, size_t len) {
  size_t done = 0;
  while (done < len) {
    ssize_t w = write(fd, data + done, len - done);
    if (w < 0) {
      if (errno == EINTR) continue;
      return -1;
    }
    done += (size_t)w;
  }
  return 0;
}

int write_atomic(const char *path, const char *data, size_t len, mode_t mode) {
  char tmp[512];
  if (snprintf(tmp, sizeof tmp, "%s.tmp", path) >= (int)sizeof tmp) return -1;
  int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, mode);
  if (fd < 0) return -1;
  if (write_all(fd, data, len) != 0 || fsync(fd) != 0) {
    close(fd);
    unlink(tmp);
    return -1;
  }
  if (close(fd) != 0) {
    unlink(tmp);
    return -1;
  }
  if (rename(tmp, path) != 0) {
    unlink(tmp);
    return -1;
  }
  return 0;
}

/* PID 1's start time is field 22 of /proc/1/stat, after a comm field that may hold spaces. */
static int pid1_start(unsigned long long *out) {
  char buf[1024];
  if (read_small_file("/proc/1/stat", buf, sizeof buf) < 0) return -1;
  char *close_paren = strrchr(buf, ')');
  if (close_paren == NULL) return -1;
  /* After ") " comes field 3; start time is field 22, so it is the 20th field from there. */
  char *p = close_paren + 1;
  for (int field = 3; field < 22; field++) {
    while (*p == ' ') p++;
    while (*p != ' ' && *p != '\0') p++;
    if (*p == '\0') return -1;
  }
  *out = strtoull(p, NULL, 10);
  return 0;
}

int generation_now(char *out, size_t n) {
  char boot[64];
  if (read_small_file("/proc/sys/kernel/random/boot_id", boot, sizeof boot) < 0) return -1;
  char *nl = strchr(boot, '\n');
  if (nl != NULL) *nl = '\0';
  unsigned long long start = 0;
  if (pid1_start(&start) != 0) return -1;
  int written = snprintf(out, n, "%s %llu", boot, start);
  return (written > 0 && (size_t)written < n) ? 0 : -1;
}

int generation_await(const char *want, int timeout_ms) {
  char seen[GEN_MAX];
  for (int waited = 0;; waited += 50) {
    if (read_small_file(STATE "/generation", seen, sizeof seen) > 0) {
      return strcmp(seen, want) == 0 ? 0 : -1;
    }
    if (waited >= timeout_ms) return -1;
    struct timespec ts = {.tv_sec = 0, .tv_nsec = 50 * 1000 * 1000};
    nanosleep(&ts, NULL);
  }
}
