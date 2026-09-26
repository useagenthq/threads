/* Records: one JSON file per key-hash, written atomically, read back by the host. */
#define _GNU_SOURCE
#include "supervise.h"

#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* The record fields are hex, decimal or a fixed word, so no JSON value ever needs escaping.
   Anything else would be a bug in this program, not input we should try to encode. */
static int is_plain(const char *s) {
  for (const char *p = s; *p != '\0'; p++) {
    if (*p < 0x20 || *p == '"' || *p == '\\' || *p == 0x7f) return 0;
  }
  return 1;
}

int record_write(const struct record *r) {
  if (!is_plain(r->key) || !is_plain(r->generation) || !is_plain(r->state)) return -1;
  char json[1024];
  int n = snprintf(json, sizeof json,
                   "{\"key\":\"%s\",\"generation\":\"%s\",\"state\":\"%s\",\"child_pid\":%ld,"
                   "\"supervisor_pid\":%ld,\"supervisor_start\":%llu,\"deadline_ms\":%lld,"
                   "\"exit_code\":%d}\n",
                   r->key, r->generation, r->state, r->child_pid, r->supervisor_pid,
                   r->supervisor_start, r->deadline_ms, r->exit_code);
  if (n <= 0 || (size_t)n >= sizeof json) return -1;
  char path[512];
  if (state_path(path, sizeof path, "records", r->key) != 0) return -1;
  /* The name carries ".json" so an archive GET of state/ reads as a directory of records. */
  if (strlen(path) + 5 >= sizeof path) return -1;
  strcat(path, ".json");
  return write_atomic(path, json, (size_t)n, 0600);
}

/* Finds "<name>":  and returns the first byte of its value, or NULL. */
static const char *field(const char *json, const char *name) {
  char needle[32];
  if (snprintf(needle, sizeof needle, "\"%s\":", name) >= (int)sizeof needle) return NULL;
  const char *at = strstr(json, needle);
  return at == NULL ? NULL : at + strlen(needle);
}

static void copy_string_field(const char *json, const char *name, char *out, size_t n) {
  out[0] = '\0';
  const char *v = field(json, name);
  if (v == NULL || *v != '"') return;
  v++;
  const char *end = strchr(v, '"');
  if (end == NULL) return;
  size_t len = (size_t)(end - v);
  if (len >= n) return;
  memcpy(out, v, len);
  out[len] = '\0';
}

static long long number_field(const char *json, const char *name) {
  const char *v = field(json, name);
  return v == NULL ? 0 : strtoll(v, NULL, 10);
}

int record_read(const char *key, struct record *out) {
  char path[512];
  if (state_path(path, sizeof path, "records", key) != 0) return -1;
  if (strlen(path) + 5 >= sizeof path) return -1;
  strcat(path, ".json");
  char json[1024];
  if (read_small_file(path, json, sizeof json) <= 0) return -1;
  memset(out, 0, sizeof *out);
  copy_string_field(json, "key", out->key, sizeof out->key);
  copy_string_field(json, "generation", out->generation, sizeof out->generation);
  copy_string_field(json, "state", out->state, sizeof out->state);
  out->child_pid = (long)number_field(json, "child_pid");
  out->supervisor_pid = (long)number_field(json, "supervisor_pid");
  out->supervisor_start = (unsigned long long)number_field(json, "supervisor_start");
  out->deadline_ms = number_field(json, "deadline_ms");
  out->exit_code = (int)number_field(json, "exit_code");
  if (out->state[0] == '\0' || out->key[0] == '\0') return -1;
  return 0;
}

/* A running or stuck record from an older generation reads as terminated: the container
   stopped, so every process in it died. Such a record is never rewritten. */
int record_is_live(const struct record *r, const char *generation) {
  if (strcmp(r->generation, generation) != 0) return 0;
  return strcmp(r->state, "running") == 0 || strcmp(r->state, "stuck") == 0;
}

int records_any_live(const char *generation) {
  DIR *dir = opendir(STATE "/records");
  if (dir == NULL) return -1;
  int live = 0;
  struct dirent *entry;
  while (live == 0 && (entry = readdir(dir)) != NULL) {
    const char *name = entry->d_name;
    size_t len = strlen(name);
    if (len <= 5 || strcmp(name + len - 5, ".json") != 0) continue;
    char key[KEY_MAX + 1];
    if (len - 5 > KEY_MAX) continue;
    memcpy(key, name, len - 5);
    key[len - 5] = '\0';
    struct record r;
    if (record_read(key, &r) == 0 && record_is_live(&r, generation)) live = 1;
  }
  closedir(dir);
  return live;
}
