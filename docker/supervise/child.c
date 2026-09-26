/* The command child: its own session, uid 1000, no environment, no lock fd. */
#define _GNU_SOURCE
#include "supervise.h"

#include <fcntl.h>
#include <grp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int open_stdin(const char *key, int with_stdin) {
  if (!with_stdin) return open("/dev/null", O_RDONLY);
  char path[512];
  if (state_path(path, sizeof path, "stdin", key) != 0) return -1;
  return open(path, O_RDONLY);
}

/* Runs in the forked child and never returns. */
void child_main(const char *key, int with_stdin, char *const argv[], int lock_fd, int ack_fd,
                int go_fd) {
  /* Closed first: a supervisor that dies before "go" must leave the lock free. */
  close(lock_fd);
  int fd0 = open_stdin(key, with_stdin);
  if (fd0 < 0 || setsid() < 0) _exit(EX_ERROR);
  if (dup2(fd0, STDIN_FILENO) < 0) _exit(EX_ERROR);
  if (fd0 != STDIN_FILENO) close(fd0);
  if (setgroups(0, NULL) != 0) _exit(EX_ERROR);
  if (setresgid(CMD_GID, CMD_GID, CMD_GID) != 0) _exit(EX_ERROR);
  if (setresuid(CMD_UID, CMD_UID, CMD_UID) != 0) _exit(EX_ERROR);
  if (write(ack_fd, "a", 1) != 1) _exit(EX_ERROR);
  close(ack_fd);
  char go = 0;
  ssize_t got = read(go_fd, &go, 1);
  /* EOF: the supervisor died before it recorded us, so nothing may run. */
  if (got != 1) _exit(EX_OK);
  close(go_fd);
  /* The exec's environment is passed through: both kits carry the call's env in it and then
     run the command under `env -i`, so the image's own env never reaches the command. */
  execvp(argv[0], argv);
  _exit(127);
}
