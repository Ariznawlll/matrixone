# Data Branch Issue BVT Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add complete, issue-traceable BVT coverage for twelve recently verified Data Branch regressions.

**Architecture:** Add one focused `.sql/.result` pair per issue under the existing Data Branch edge suite. The SQL cases cover every public acceptance path, while the exact internal failure injection for #26081, #26128, and #26205 remains in the existing Go tests and is run alongside the BVT.

**Tech Stack:** MatrixOne SQL, mo-tester BVT directives, Go tests, MatrixOne standalone launch configuration.

## Global Constraints

- Base every change on `origin/main@a4389a72f71c10663e039f0464a44749624950dd`.
- Put `-- @bvt:issue#NNNNN` immediately before each executable issue block and close it with `-- @bvt:issue`.
- Do not encode the still-failing embedded-backtick dependent-view path from #26127 as expected success.
- Do not claim SQL injects the internal callback/copy/cyclic-metadata failures for #26081, #26128, or #26205.
- Assert deterministic rows, counts, hexadecimal bytes, and Boolean identity comparisons; never baseline generated IDs, timestamps, or output file names.
- Every case cleans accounts, databases, snapshots, and generated output so it is repeatable.

---

### Task 1: Quoted identifiers and snapshot-range failures

**Files:**
- Create: `test/distributed/cases/git4data/branch/edge/issue_26071_quoted_primary_keys.sql`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26071_quoted_primary_keys.result`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26073_backslash_metadata.sql`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26073_backslash_metadata.result`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26078_reversed_snapshot_range.sql`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26078_reversed_snapshot_range.result`

**Interfaces:**
- Consumes: DATA BRANCH CREATE/DIFF/PICK/MERGE and `mo_catalog` metadata.
- Produces: isolated BVT cases discoverable by issue number.

- [ ] **Step 1: Add #26071 quoted-key operations**

Create sibling branches with a reserved-word single PK and a
reserved-word/punctuation composite PK. Execute DIFF summary, PICK an update
and deletion while leaving an insert unselected, then MERGE and query exact
destination rows.

- [ ] **Step 2: Add #26073 byte-preserving metadata assertions**

Create quoted database/table names containing backslashes, compare
`HEX(mo_tables.reldatabase/relname)` with the matching branch protection
snapshot, run DIFF, drop the child, and assert both metadata cleanup and source
readability.

- [ ] **Step 3: Add #26078 rejected and accepted ranges**

Create early/late snapshots around INSERT, UPDATE, and DELETE. Use
`-- @regex("snapshot range.*invalid|start.*after.*end",true)` for the reversed
PICK, verify the target and metadata are unchanged, verify the session remains
usable, then execute the forward range and assert the exact final rows.

- [ ] **Step 4: Generate and verify baselines**

Run:

```bash
cd /Users/ariznawl/code/mo-tester
./run.sh -m genrs -n -g -p /private/tmp/matrixone-bvt-data-branch-issues-f547/test/distributed/cases/git4data/branch/edge/issue_26071_quoted_primary_keys.sql
./run.sh -m genrs -n -g -p /private/tmp/matrixone-bvt-data-branch-issues-f547/test/distributed/cases/git4data/branch/edge/issue_26073_backslash_metadata.sql
./run.sh -m genrs -n -g -p /private/tmp/matrixone-bvt-data-branch-issues-f547/test/distributed/cases/git4data/branch/edge/issue_26078_reversed_snapshot_range.sql
```

Expected: all three commands exit 0 and create matching `.result` files.

### Task 2: Bulk accounting and target-account quota

**Files:**
- Create: `test/distributed/cases/git4data/branch/edge/issue_26081_hashmap_bulk.sql`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26081_hashmap_bulk.result`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26114_target_quota.sql`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26114_target_quota.result`

**Interfaces:**
- Consumes: `generate_series`, branch summary/PICK/MERGE, feature-limit functions, mo-tester sessions.
- Produces: public SQL controls for the hashmap consumer and cross-account quota ownership.

- [ ] **Step 1: Add #26081 mixed bulk workload**

Insert 50,000 base rows with `generate_series`, update 10,000, delete 10,000,
and insert 10,000 on the branch. Assert the exact DIFF summary, select 2,500
keys from each change family for PICK, check selected/unselected boundaries,
MERGE, compare count/sums, and confirm the source remains unchanged.

- [ ] **Step 2: Add #26114 quota-zero and ownership paths**

Create a target account, set its branch quota to zero, and attempt table- and
database-level `TO ACCOUNT` creation. Assert errors, no target objects, and no
active metadata. Raise the quota, create both forms successfully, read them as
the target account, and assert branch metadata `creator` equals the target
account ID.

- [ ] **Step 3: Preserve the exact internal failure tests**

Run:

```bash
go test ./pkg/frontend/databranchutils \
  -run 'TestBranchHashmapPopByVectorsStream.*CallbackError' -count=3
go test -race ./pkg/frontend/databranchutils \
  -run 'TestBranchHashmapPopByVectorsStream.*CallbackError' -count=1
```

Expected: both commands exit 0.

- [ ] **Step 4: Generate and verify baselines**

Run mo-tester `-m genrs` for both new SQL files and then rerun them without
`-m genrs`. Expected: both focused BVT cases pass.

### Task 3: Hash, internal-looking, and embedded-backtick identifiers

**Files:**
- Create: `test/distributed/cases/git4data/branch/edge/issue_26118_hash_identifiers.sql`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26118_hash_identifiers.result`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26121_internal_names.sql`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26121_internal_names.result`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26127_embedded_backticks.sql`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26127_embedded_backticks.result`

**Interfaces:**
- Consumes: database CLONE/Data Branch, FK/view/index/fulltext metadata.
- Produces: exact identifier round-trip, dependency, isolation, and cleanup coverage.

- [ ] **Step 1: Add #26118 `#` dependency matrix**

Put `#` in source database, parent/child table, FK constraint, and two-level
view names. Run Data Branch and CLONE; assert joins, view rows, destination-local
FK metadata, rejected invalid child writes, legal isolated writes, cleanup, and
same-name recreation.

- [ ] **Step 2: Add #26121 internal-looking user objects**

Cover `__mo_tmp_*`, `__mo_account_lock`, and `mo_increment_columns` with unique
indexes and a dependent view. Include a fulltext table as the genuine-internal
control. Assert user objects and indexes survive Data Branch/CLONE, fulltext
queries work, and an extra internal-looking user table blocks branch database
deletion without losing its row.

- [ ] **Step 3: Add #26127 repaired boundary**

Use an embedded backtick in a source table name. Cover table CLONE, table Data
Branch, database CLONE, and database Data Branch; separately use ordinary names
for FK/view dependencies and verify rejection/isolation. Do not add the
embedded-backtick view-name path until the issue is fully fixed.

- [ ] **Step 4: Generate and verify baselines**

Generate `.result` files, rerun all three cases, drop all created objects, and
rerun once more. Expected: both runs pass without leaked state.

### Task 4: Historical identity, output, vectors, and deep lineage

**Files:**
- Create: `test/distributed/cases/git4data/branch/edge/issue_26120_snapshot_identity.sql`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26120_snapshot_identity.result`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26128_output.sql`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26128_output.result`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26136_vectors.sql`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26136_vectors.result`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26205_deep_lineage.sql`
- Create: `test/distributed/cases/git4data/branch/edge/issue_26205_deep_lineage.result`

**Interfaces:**
- Consumes: snapshot catalog queries, DATA BRANCH output forms, vector types, long lineage.
- Produces: deterministic public regressions plus Go-test handoff for internal faults.

- [ ] **Step 1: Add #26120 historical relation identity**

For table- and database-level branches, snapshot a source, drop/recreate the
same name with different data, create from the snapshot, assert historical
data, assert parent/protection IDs equal the snapshot ID and differ from the
current ID, then run DIFF successfully.

- [ ] **Step 2: Add #26128 output correctness**

Use strings containing comma, double quote, backslash, Chinese text, and an
embedded newline. Assert `OUTPUT AS` exact change rows and non-branch metadata,
run `OUTPUT FILE` through a deterministic resource directory while ignoring
the generated filename, and assert source/branch immutability.

- [ ] **Step 3: Add #26136 vector matrix**

Cover VECF32, VECF64, VECBF16, VECF16, VECINT8, and VECUINT8 with numeric
boundaries, NULL before non-NULL rows, NULL/value transitions, and mixed
INSERT/UPDATE/DELETE. Assert exact text and NULL state after CREATE, DIFF,
selective PICK, and MERGE.

- [ ] **Step 4: Add #26205 deep valid lineage**

Create sixteen branch generations, mutate both a descendant and a sibling,
assert reverse DIFF summaries, merge into an independent destination, and
assert the exact row set and sixteen active metadata nodes.

- [ ] **Step 5: Preserve exact white-box error tests**

Run:

```bash
go test ./pkg/frontend \
  -run 'TestSubmitCSVBatchForConversionReturnsCopyErrorBeforeSubmit' -count=3
go test ./pkg/frontend/databranchutils \
  -run 'TestNewDAGCycle' -count=3
go test -race ./pkg/frontend/databranchutils \
  -run 'TestNewDAGCycle' -count=1
```

Expected: every command exits 0.

- [ ] **Step 6: Generate and verify baselines**

Generate all four `.result` files, then run the four SQL cases twice. Expected:
all eight focused executions pass and cleanup makes the second run independent.

### Task 5: Coverage audit and final verification

**Files:**
- Verify: `test/distributed/cases/git4data/branch/edge/issue_*.sql`
- Verify: `test/distributed/cases/git4data/branch/edge/issue_*.result`

**Interfaces:**
- Consumes: all twelve BVT cases and three focused Go test groups.
- Produces: reviewable evidence that every issue is discoverable and executable.

- [ ] **Step 1: Audit issue markers**

Run:

```bash
for n in 26071 26073 26078 26081 26114 26118 26120 26121 26127 26128 26136 26205; do
  git grep -q -- "-- @bvt:issue#$n" -- test/distributed/cases || exit 1
done
```

Expected: exit 0.

- [ ] **Step 2: Run all focused BVT files**

Run mo-tester against every `issue_*.sql` added by this plan. Expected: twelve
passed cases and zero failed cases.

- [ ] **Step 3: Run focused Go regressions**

Run the commands from Tasks 2 and 4. Expected: every normal and race run exits
0.

- [ ] **Step 4: Check the patch**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only the design, plan, and intended BVT files
are changed.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers \
  test/distributed/cases/git4data/branch/edge/issue_*.sql \
  test/distributed/cases/git4data/branch/edge/issue_*.result
git commit -m "test: add data branch issue BVT coverage"
```

