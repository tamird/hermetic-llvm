#!/usr/bin/env bash
set -euo pipefail
cd "${BUILD_WORKSPACE_DIRECTORY:?}/e2e/kernel_headers"
exec bash test_errors.sh "$@"
