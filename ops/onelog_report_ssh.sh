#!/bin/sh
# Forced SSH command: allow only the three aggregate report windows.
case "$SSH_ORIGINAL_COMMAND" in
    morning|midday|evening)
        exec sudo /usr/local/libexec/onelog-report-snapshot "$SSH_ORIGINAL_COMMAND"
        ;;
    *)
        exit 64
        ;;
esac
