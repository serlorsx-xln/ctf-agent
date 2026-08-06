#!/usr/bin/env bash
# Cross-platform entry: bash scripts/install.sh  OR  powershell -File scripts/install.ps1
exec bash "$(dirname "$0")/scripts/install.sh" "$@"
