/* The one-sweep tree kill, and reading a process's start time. */
#define _GNU_SOURCE
#include "supervise.h"

#include <dirent.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

unsigned long long proc_start_time(pid_t pid) {
  char path[64];
  if (snprintf(path, sizeof path, "/proc/%ld/stat", (long)pid) >= (int)sizeof path) return 0;
  char buf[1024];
  if (read_small_file(path, buf, sizeof buf) < 0) return 0;
  char *p = strrchr(buf, ')');
  if (p == NULL) return 0;
  p++;
  for (int f = 3; f < 22; f++) {
    while (*p == ' ') p++;
    while (*p != ' ' && *p != '\0') p++;
    if (*p == '\0') return 0;
  }
  return strtoull(p, NULL, 10);
}

/* A zombie holds no memory and runs no code; it disappears when its parent reaps it, so it
   is not a survivor. Counting one would make every killed command look `stuck`. */
static int is_command_uid(const char *pid_name) {
  char path[64];
  if (snprintf(path, sizeof path, "/proc/%s/status", pid_name) >= (int)sizeof path) return 0;
  char buf[4096];
  if (read_small_file(path, buf, sizeof buf) < 0) return 0;
  const char *state = strstr(buf, "\nState:");
  if (state != NULL) {
    const char *letter = state + 7;
    while (*letter == ' ' || *letter == '\t') letter++;
    if (*letter == 'Z') return 0;
  }
  const char *uid = strstr(buf, "\nUid:");
  if (uid == NULL) return 0;
  return strtol(uid + 5, NULL, 10) == CMD_UID;
}

static int any_command_process(void) {
  DIR *dir = opendir("/proc");
  if (dir == NULL) return 0;
  int found = 0;
  struct dirent *entry;
  long self = (long)getpid();
  while (found == 0 && (entry = readdir(dir)) != NULL) {
    char *end = NULL;
    long pid = strtol(entry->d_name, &end, 10);
    if (pid <= 0 || end == NULL || *end != '\0' || pid == self) continue;
    if (is_command_uid(entry->d_name)) found = 1;
  }
  closedir(dir);
  return found;
}

/* One kernel sweep, not a /proc race: as uid 1000 we may signal exactly the command's
   processes, kill(-1) reaches every one of them at once (pending fatal signals block new
   forks, so a fork bomb can't outrun it), and the saved uid takes us back to root. */
static void sweep_once(void) {
  if (setresuid(CMD_UID, CMD_UID, 0) != 0) die("cannot drop to the command uid for the sweep");
  kill(-1, SIGKILL);
  if (setresuid(0, 0, 0) != 0) _exit(EX_ERROR);
}

int sweep_command_processes(int deadline_s) {
  for (int waited_ms = 0;; waited_ms += 50) {
    sweep_once();
    if (!any_command_process()) return 0;
    if (waited_ms >= deadline_s * 1000) return -1;
    struct timespec ts = {.tv_sec = 0, .tv_nsec = 50 * 1000 * 1000};
    nanosleep(&ts, NULL);
  }
}
