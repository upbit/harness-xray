#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PRELOAD="$SCRIPT_DIR/anthropic-dump-preload.cjs"

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <command> [args...]" >&2
  exit 64
fi

case " ${NODE_OPTIONS-} " in
  *" --require=$PRELOAD "*) ;;
  *)
    if [ -n "${NODE_OPTIONS-}" ]; then
      export NODE_OPTIONS="$NODE_OPTIONS --require=$PRELOAD"
    else
      export NODE_OPTIONS="--require=$PRELOAD"
    fi
    ;;
esac

exec "$@"
