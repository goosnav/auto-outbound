#!/bin/sh
release_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
/bin/sh "$release_root/Open Auto Outbound.sh"
result=$?
if [ "$result" -ne 0 ] && [ -t 0 ]; then
  printf '\n%s' 'The app could not start. Press Return to close. '
  read -r answer
fi
exit "$result"
