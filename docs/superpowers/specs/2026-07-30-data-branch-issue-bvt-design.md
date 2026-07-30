# Data Branch Issue BVT Design

## Goal

Add explicit MatrixOne BVT coverage for issues #26071, #26073, #26078,
#26081, #26114, #26118, #26120, #26121, #26127, #26128, #26136, and
#26205. Coverage must exercise the complete public SQL behavior verified
during QA, not merely attach issue markers to loosely related smoke tests.

## Scope and organization

The cases will live under `test/distributed/cases/git4data/branch/edge`.
Each issue gets a clearly delimited `-- @bvt:issue#NNNNN` block so a grep by
issue number finds the owning regression. Closely related setup can be shared,
but each block has its own assertions and cleanup.

Existing cases are reused only when they already exercise the exact acceptance
path. Missing assertions are added rather than represented by an issue tag
alone. Test output must be deterministic: assert exact rows, counts, metadata
identity, hexadecimal identifier bytes, and expected errors; do not assert
unstable object IDs or timestamps.

## Coverage matrix

| Issue | BVT coverage |
| --- | --- |
| #26071 | Single and composite quoted primary keys across sibling DIFF, selective PICK, deletion, and MERGE. |
| #26073 | Backslash-bearing database/table identifiers; compare catalog and protection metadata with `HEX`, consume metadata with DIFF, and verify cleanup. |
| #26078 | Reversed snapshot range rejection, failure atomicity, session reuse, then forward-range INSERT/UPDATE/DELETE control. |
| #26081 | Public bulk path with mixed UPDATE/DELETE/INSERT; exact DIFF summary, selected/unselected PICK boundaries, full MERGE, and source immutability. |
| #26114 | Target quota 0 rejection for cross-account table/database branch, no residual object/metadata, successful target-owned creation after quota increase. |
| #26118 | `#` in database/table/constraint/dependent-view names across database Data Branch and CLONE; data, FK enforcement, view dependency, isolation, and repeat cleanup. |
| #26120 | Table/database snapshot branches after drop/recreate of the same source name; historical rows, parent/protection IDs, and terminal DIFF. |
| #26121 | Internal-looking user table names across Data Branch and CLONE; table/data/index/view preservation, genuine fulltext-internal control, and delete-database rejection. |
| #26127 | The repaired embedded-backtick table paths for table/database CLONE and Data Branch, plus ordinary dependency/FK isolation. The still-broken embedded-backtick dependent-view path is not encoded as success. |
| #26128 | Public `OUTPUT AS`/`OUTPUT FILE` path with commas, quotes, backslashes, Chinese text, and embedded newlines; exact change rows and source immutability. |
| #26136 | All six vector families, boundary values, NULL transitions, non-first logical rows, mixed INSERT/UPDATE/DELETE, DIFF/PICK/MERGE. |
| #26205 | Public 16-level valid lineage control with reverse DIFF/PICK/MERGE and bounded completion. |

## White-box-only failure boundaries

Three original defects require internal error injection or invalid in-memory
state that public SQL cannot create:

- #26081 callback failure inside `BranchHashmap.PopByVectorsStream`;
- #26128 `batch.Dup` failure before CSV worker submission;
- #26205 cyclic `DataBranchMetadata` passed directly to `NewDAG`.

Their exact failure paths remain covered by focused Go tests. BVT covers the
complete corresponding public SQL workflows at enough volume/depth to exercise
the same components. The issue comments and test names must distinguish these
two layers; BVT must not claim to inject an error that it cannot produce.

## Execution and acceptance

Run the new BVT files against a MatrixOne binary built from the same latest
`main` commit as the branch. A case is accepted only when:

1. every issue number appears in an executable BVT block;
2. each block contains assertions for the acceptance paths above;
3. the focused BVT run passes;
4. the matching focused Go tests pass for the three white-box-only boundaries;
5. rerunning the focused BVT does not leave databases, accounts, snapshots,
   branch metadata, or output artifacts that affect the next run.

