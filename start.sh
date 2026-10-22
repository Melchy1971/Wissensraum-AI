#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
    if ! .venv/bin/python -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) and sys.version_info.releaselevel == 'final' else 1)"; then
        echo "Die virtuelle Umgebung verwendet keine unterstützte Python-Version; .venv entfernen und neu starten." >&2
        exit 1
    fi
else
    PYTHON=${PYTHON:-python3}
    if ! "$PYTHON" -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) and sys.version_info.releaselevel == 'final' else 1)"; then
        echo "Python 3.11 bis 3.13 wird benötigt; Vorabversionen werden nicht unterstützt." >&2
        exit 1
    fi
    "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
exec .venv/bin/python app.py
