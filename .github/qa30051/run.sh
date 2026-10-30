#!/usr/bin/env bash
set -euo pipefail
mkdir -p qa-evidence "$RUNNER_TEMP/qa30051-stage"
IMAGE='ccr.ccs.tencentyun.com/matrixone-dev/matrixone@sha256:8d69616787d272d19986f48fe80ff83f056313ae1ceca94c98bc7847a2fde125'
printf 'product_commit=a54850983d9cddaacc97e38a84515ec724c937b7\nimage=%s\n' "$IMAGE" > qa-evidence/provenance.txt
git rev-parse HEAD >> qa-evidence/provenance.txt
sha256sum clients/python/matrixone/_local_infile.py >> qa-evidence/provenance.txt
uname -a >> qa-evidence/provenance.txt
python --version >> qa-evidence/provenance.txt
python -c 'import sqlalchemy,pyarrow,pymysql,platform; print(platform.machine(),sqlalchemy.__version__,pyarrow.__version__,pymysql.__version__)' >> qa-evidence/provenance.txt
docker pull --platform linux/amd64 "$IMAGE"
docker run -d --name qa30051-mo --network host --workdir / -v "$PWD/.github/qa30051:/qa-config:ro" -v "$RUNNER_TEMP/qa30051-stage:$RUNNER_TEMP/qa30051-stage" --entrypoint /mo-service "$IMAGE" -debug-http=:12345 -launch /qa-config/launch.toml
python .github/qa30051/bootstrap.py | tee qa-evidence/bootstrap.log
python -m pytest --import-mode=importlib -q clients/python/tests/offline/contracts/test_local_infile.py clients/python/tests/offline/contracts/test_wave2_boundaries.py clients/python/tests/offline/test_connection_contracts.py | tee qa-evidence/offline.log
python .github/qa30051/guard_lifetime_probe.py guard-preferred | tee qa-evidence/lifetime.json
python .github/qa30051/guard_async_probe.py | tee qa-evidence/async-lifetime.json
python .github/qa30051/diagnose.py "$RUNNER_TEMP/qa30051-stage" | tee qa-evidence/profile-summary.log
python .github/qa30051/check-cleanup.py | tee qa-evidence/cleanup.json
