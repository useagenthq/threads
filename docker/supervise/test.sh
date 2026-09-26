#!/usr/bin/env bash
# D1's proof: the supervisor's behaviour inside a real container.
# Usage: docker/supervise/test.sh [image]   (needs scripts/build-supervisor.sh to have run)
set -uo pipefail

docker="${DOCKER:-docker}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
image="${1:-node:22-bookworm}"
name="threads-supervise-test"
supervise=/run/threads/bin/supervise
arch=$("$docker" version --format '{{.Server.Arch}}')
binary="$root/docker/supervise/dist/linux-$arch/supervise"
fails=0

ok() { echo "  ok   $1"; }
bad() { echo "  FAIL $1"; fails=$((fails + 1)); }
is() { # is <label> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1: expected [$2], got [$3]"; fi
}
has() { # has <label> <needle> <haystack>
  case "$3" in *"$2"*) ok "$1" ;; *) bad "$1: [$3] does not contain [$2]" ;; esac
}
in_box() { "$docker" exec "$name" "$@" 2>&1; }
as_command_uid() { "$docker" exec -u 1000 "$name" "$@" 2>&1; }
record() { in_box cat "/run/threads/state/records/$1.json"; }
state_of() { record "$1" | sed -n 's/.*"state":"\([a-z]*\)".*/\1/p'; }

[ -x "$binary" ] || { echo "no $binary: run scripts/build-supervisor.sh first"; exit 1; }

start_container() {
  "$docker" rm -f "$name" >/dev/null 2>&1
  "$docker" volume rm -f "threads-exec-$name" "threads-ws-$name" >/dev/null 2>&1
  "$docker" create --name "$name" --init --user 0 --entrypoint "$supervise" \
    --read-only --tmpfs /tmp --network none \
    --cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add KILL --cap-add CHOWN \
    --security-opt no-new-privileges --pids-limit 1024 --no-healthcheck \
    -v "threads-exec-$name:/run/threads" \
    --mount "type=volume,source=threads-ws-$name,target=/workspace,volume-nocopy=true" \
    "$image" --idle >/dev/null || exit 1
  # The adapter's archive PUT: uid/gid 0, mode 0700, so uid 1000 can neither read nor run it.
  local stage
  stage=$(mktemp -d)
  mkdir -p "$stage/bin" "$stage/state"
  cp "$binary" "$stage/bin/supervise"
  chmod 0700 "$stage/bin/supervise" "$stage/bin" "$stage/state"
  xattr -rc "$stage" 2>/dev/null
  COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata --uid 0 --gid 0 --uname root \
    --gname root -cf - -C "$stage" bin state | "$docker" cp - "$name:/run/threads/"
  rm -rf "$stage"
  "$docker" start "$name" >/dev/null
  # Admission waits for state/generation, so there is nothing to poll for here.
}

echo "== the image is usable and /workspace belongs to the command uid"
start_container
has "--check reports the image's tools" '"sh":true' "$(in_box $supervise --check)"
is "/workspace is the command uid's" "1000:1000 755" "$(in_box stat -c '%u:%g %a' /workspace)"

echo "== a command runs as uid 1000 with exactly its own env"
is "the command is uid 1000" "1000" "$(in_box $supervise aa01 5000 id -u)"
is "env -i gives exactly the call's env" "FOO=bar" \
  "$(in_box $supervise aa02 5000 env -i FOO=bar printenv)"
is "the record settles as exited" "exited" "$(state_of aa01)"

echo "== uid 1000 sees neither the state directory nor root's environment"
has "state is root-only" "Permission denied" "$(as_command_uid ls /run/threads/state)"
has "/proc/1/environ is root-only" "Permission denied" \
  "$(in_box $supervise aa03 5000 sh -c 'cat /proc/1/environ')"
has "the supervisor's binary is root-only" "ermission denied" "$(as_command_uid $supervise --check)"

echo "== the child holds no lock fd"
# The command is the child, so it can read its own fd directory; root cannot read anyone
# else's, because every capability but the supervisor's four is dropped.
is "the command's open files are only its three streams" "0 1 2" \
  "$(in_box $supervise ab00 5000 sh -c 'ls /proc/$$/fd | tr "\n" " "' | sed 's/ $//')"
"$docker" exec -d "$name" $supervise ab01 30000 sh -c 'sleep 20'
sleep 2
"$docker" exec "$name" $supervise --terminate ab01 >/dev/null 2>&1
sleep 1
is "the terminated command's record says so" "terminated" "$(state_of ab01)"

echo "== one sweep kills a setsid escape and a fork bomb"
in_box $supervise ac01 5000 sh -c 'setsid sh -c "sleep 60 &"; sleep 0.2' >/dev/null
is "no escaped process survives the command" "none" \
  "$(in_box sh -c 'ps -eo args | grep "[s]leep 60" || echo none')"
in_box $supervise ac02 3000 sh -c ':(){ :|:& };:' >/dev/null
is "the fork bomb's record settles" "exited" "$(state_of ac02)"
is "a command runs after the fork bomb" "alive" "$(in_box $supervise ac03 3000 echo alive)"

echo "== the deadline terminates the command"
in_box $supervise ad01 1000 sh -c 'sleep 30' >/dev/null
is "a command past its deadline is terminated" "terminated" "$(state_of ad01)"
is "admission accepts once the record is settled" "again" "$(in_box $supervise ad02 3000 echo again)"

echo "== stdin reaches the command"
stdin_stage=$(mktemp -d)
mkdir -p "$stdin_stage/state/stdin"
printf 'payload' > "$stdin_stage/state/stdin/ae01"
xattr -rc "$stdin_stage" 2>/dev/null
COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata --uid 0 --gid 0 --uname root --gname root \
  -cf - -C "$stdin_stage" state | "$docker" cp - "$name:/run/threads/"
rm -rf "$stdin_stage"
is "the staged stdin is the command's fd 0" "payload" "$(in_box $supervise ae01 5000 --stdin cat)"

echo "== a dead supervisor refuses admission, and the probe stops the container"
"$docker" exec -d "$name" $supervise af01 60000 sh -c 'sleep 45'
sleep 2
pid=$(record af01 | sed -n 's/.*"supervisor_pid":\([0-9]*\).*/\1/p')
in_box sh -c "kill -9 $pid"
sleep 1
refusal=$(in_box $supervise af02 3000 echo NOPE)
has "admission is refused while a stale record is running" "admission refused" "$refusal"
in_box $supervise --terminate af01 >/dev/null
sleep 3
is "the probe stopped the container" "exited" "$("$docker" inspect "$name" --format '{{.State.Status}}')"

echo "== after a restart every earlier record reads as an older generation"
before=$(in_box cat /run/threads/state/generation)
"$docker" start "$name" >/dev/null
sleep 2
after=$(in_box cat /run/threads/state/generation)
if [ "$before" != "$after" ]; then ok "the generation changed"; else bad "the generation did not change"; fi
is "admission accepts after the restart" "ok" "$(in_box $supervise af03 3000 echo ok)"
is "the stale record is never rewritten" "running" "$(state_of af01)"

"$docker" rm -f "$name" >/dev/null 2>&1
"$docker" volume rm -f "threads-exec-$name" "threads-ws-$name" >/dev/null 2>&1

echo
if [ "$fails" -eq 0 ]; then echo "supervisor: all checks passed"; else echo "supervisor: $fails failed"; fi
exit "$fails"
