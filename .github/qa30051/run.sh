#!/usr/bin/env bash
set -euo pipefail
mkdir -p qa-evidence "$RUNNER_TEMP/qa30051-stage"
IMAGE='ccr.ccs.tencentyun.com/matrixone-dev/matrixone@sha256:8d69616787d272d19986f48fe80ff83f056313ae1ceca94c98bc7847a2fde125'
printf 'product_commit=a54850983d9cddaacc97e38a84515ec724c937b7\nimage=%s\n' "$IMAGE" > qa-evidence/provenance.txt
git rev-parse HEAD >> qa-evidence/provenance.txt
sha256sum clients/python/matrixone/_local_infile.py clients/python/matrixone/analytics.py clients/python/matrixone/async_analytics.py >> qa-evidence/provenance.txt
python -c "import os; assert 'MALLOC_ARENA_MAX' not in os.environ; print('MALLOC_ARENA_MAX=unset')" >> qa-evidence/provenance.txt
uname -a >> qa-evidence/provenance.txt
python --version >> qa-evidence/provenance.txt
python -c 'import sqlalchemy,pyarrow,pymysql,platform; print(platform.machine(),sqlalchemy.__version__,pyarrow.__version__,pymysql.__version__)' >> qa-evidence/provenance.txt
pulled=0
for attempt in 1 2 3; do
  if docker pull --platform linux/amd64 "$IMAGE" > "$RUNNER_TEMP/qa30051-image-pull.log" 2>&1; then
    pulled=1
    break
  fi
  echo "Image pull failed on attempt $attempt; retrying."
  sleep 5
done
if [ "$pulled" != 1 ]; then echo 'Image pull failed after 3 attempts'; exit 1; fi
docker run -d --name qa30051-mo --network host --workdir / -v "$PWD/.github/qa30051:/qa-config:ro" -v "$RUNNER_TEMP/qa30051-stage:$RUNNER_TEMP/qa30051-stage" --entrypoint /mo-service "$IMAGE" -debug-http=:12345 -launch /qa-config/launch.toml
python .github/qa30051/bootstrap.py | tee qa-evidence/bootstrap.log
python -m pytest --import-mode=importlib -q clients/python/tests/offline/contracts/test_local_infile.py clients/python/tests/offline/contracts/test_wave2_boundaries.py clients/python/tests/offline/test_connection_contracts.py | tee qa-evidence/offline.log
python .github/qa30051/guard_lifetime_probe.py guard-preferred | tee qa-evidence/lifetime.json
python .github/qa30051/guard_async_probe.py | tee qa-evidence/async-lifetime.json
for n in 1 2 3; do
  python .github/qa30051/live-lifetime.py > "qa-evidence/live-lifetime-$n.json"
  python .github/qa30051/async-stream-lifetime.py > "qa-evidence/async-stream-lifetime-$n.json"
done
MO_ANALYTICS_ONLINE=1 MO_PORT=6003 MO_DATABASE=mo_analytics_test PYTHONPATH="$PWD/clients/python/tests/analytics:${PYTHONPATH:-}" python -m pytest --import-mode=importlib -q --basetemp="$RUNNER_TEMP/qa30051-stage/pytest" clients/python/tests/analytics/test_analytics.py::test_early_close_and_bound_batch clients/python/tests/analytics/test_analytics.py::test_query_errors_cleanup_and_pool_reuse clients/python/tests/analytics/test_relation_ownership.py::test_stream_source_error_keeps_precedence_over_cleanup clients/python/tests/analytics/test_relation_ownership.py::test_stream_validation_cursor_close_keeps_primary_error clients/python/tests/analytics/test_async_analytics.py::test_async_stream_exact_types_empty_result_and_early_close clients/python/tests/analytics/test_async_workers.py | tee qa-evidence/stream-contracts.log
for n in 1 2 3; do
  MO_PORT=6003 MO_DATABASE=mo_analytics_test python clients/python/tests/analytics/run_soak.py --staging-dir "$RUNNER_TEMP/qa30051-stage" --output "qa-evidence/round-$n.json" --warmup-seconds 30 --seconds 90 --workers 8 --rows 1000000 > "qa-evidence/round-$n.log" 2>&1
  python .github/qa30051/verify.py "qa-evidence/round-$n.json" | tee "qa-evidence/round-$n-summary.json"
done
python .github/qa30051/check-cleanup.py | tee qa-evidence/cleanup.json
