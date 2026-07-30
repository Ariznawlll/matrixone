-- Regression for issue #26081.
-- Public bulk control for the DATA BRANCH hashmap consumer.
-- The callback-error injection itself remains in BranchHashmap Go tests.
drop database if exists bvt_issue_26081;
create database bvt_issue_26081;
use bvt_issue_26081;

create table base(
    id int primary key,
    val bigint,
    payload varchar(32)
);
insert into base
select result, result, concat('row-', cast(result as varchar))
from generate_series(1, 50000) g;
data branch create table branch_t from base;
data branch create table pick_dst from base;
data branch create table merge_dst from base;

update branch_t
set val = val + 100000, payload = 'updated'
where id between 1 and 10000;
delete from branch_t where id between 10001 and 20000;
insert into branch_t
select result, result, concat('row-', cast(result as varchar))
from generate_series(50001, 60000) g;

data branch diff branch_t against base output summary;

create table pick_keys(id int primary key);
insert into pick_keys
select id from base
where id between 1 and 2500
   or id between 10001 and 12500;
insert into pick_keys
select id from branch_t where id between 50001 and 52500;
data branch pick branch_t into pick_dst keys(select id from pick_keys);

select count(*) as picked_rows,
       sum(id between 1 and 2500 and val = id + 100000) as selected_updates,
       sum(id between 10001 and 12500) as selected_deleted_rows,
       sum(id between 50001 and 52500) as selected_inserts,
       sum(id = 2501 and val = 2501) as unselected_update_control,
       sum(id = 12501 and val = 12501) as unselected_delete_control,
       sum(id = 52501) as unselected_insert_control
from pick_dst;

data branch merge branch_t into merge_dst;
select count(*) as rows_after_merge,
       sum(id) as id_sum_after_merge,
       sum(val) as val_sum_after_merge
from merge_dst;
select sum(id between 1 and 10000 and val = id + 100000) as updated_rows,
       sum(id between 10001 and 20000) as deleted_rows,
       sum(id between 50001 and 60000) as inserted_rows
from merge_dst;
select count(*) as base_rows,
       sum(id) as base_id_sum,
       sum(val) as base_val_sum
from base;

drop database bvt_issue_26081;
