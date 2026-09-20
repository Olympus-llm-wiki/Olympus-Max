#!/usr/bin/env bash
set -euo pipefail
# The parent delivers one existing BWS key through docker exec stdin and a
# named pipe. Key bytes stay in the pipe buffer and native process env; no
# regular secret file, Docker Config.Env value or command argument is created.
task_key_pipe=/run/olympus/api-key.pipe
umask 077
mkfifo "$task_key_pipe"
if ! HINDSIGHT_API_TENANT_API_KEY="$(timeout 60 cat "$task_key_pipe")"; then
  printf 'Runtime key delivery did not complete within 60 seconds.\n' >&2
  exit 1
fi
test -n "$HINDSIGHT_API_TENANT_API_KEY"
HINDSIGHT_CP_DATAPLANE_API_KEY="$HINDSIGHT_API_TENANT_API_KEY"
HINDSIGHT_CP_ACCESS_KEY="$HINDSIGHT_API_TENANT_API_KEY"
export HINDSIGHT_API_TENANT_API_KEY HINDSIGHT_CP_DATAPLANE_API_KEY HINDSIGHT_CP_ACCESS_KEY
rm -- "$task_key_pipe"
if [ "${1:-api}" = worker ]; then
  exec python /opt/olympus/runtime-services.py worker
fi
exec python /opt/olympus/runtime-services.py api
