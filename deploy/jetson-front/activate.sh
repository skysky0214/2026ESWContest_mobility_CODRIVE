# Usage: source deploy/jetson-front/activate.sh
CODRIVE_DEPLOY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CODRIVE_REPO_ROOT="$(cd -- "$CODRIVE_DEPLOY_DIR/../.." && pwd)"
source "$CODRIVE_REPO_ROOT/.venv/bin/activate"
export CODRIVE_REPO_ROOT
export PYTHONPATH="$CODRIVE_REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
if [[ -d "$CODRIVE_REPO_ROOT/.deps/cyclonedds/install" ]]; then
    export CYCLONEDDS_HOME="$CODRIVE_REPO_ROOT/.deps/cyclonedds/install"
fi
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
