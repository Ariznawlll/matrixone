-- Regression for issue #26205.
-- Public control: a valid deep DATA BRANCH lineage remains traversable.
-- Cyclic in-memory metadata is constructed directly by the NewDAG Go test.
drop database if exists bvt_issue_26205;
create database bvt_issue_26205;
use bvt_issue_26205;

create table b0(id int primary key, val int);
insert into b0 values (1, 10), (2, 20);
data branch create table b1 from b0;
update b1 set val = 11 where id = 1;
data branch create table b2 from b1;
insert into b2 values (3, 30);
data branch create table b3 from b2;
delete from b3 where id = 2;
data branch create table b4 from b3;
data branch create table b5 from b4;
data branch create table b6 from b5;
data branch create table b7 from b6;
data branch create table b8 from b7;
data branch create table b9 from b8;
data branch create table b10 from b9;
data branch create table b11 from b10;
data branch create table b12 from b11;
data branch create table b13 from b12;
data branch create table b14 from b13;
data branch create table b15 from b14;
data branch create table b16 from b15;

data branch create table sibling from b8;
update sibling set val = 88 where id = 1;
insert into sibling values (5, 50);
update b16 set val = 33 where id = 3;
insert into b16 values (4, 40);
data branch create table merge_dst from b0;
data branch create table pick_dst from b0;

data branch diff b16 against b0 output summary;
data branch diff b16 against sibling output summary;
data branch pick b16 into pick_dst keys(1, 2, 3, 4);
data branch merge b16 into merge_dst;

select id, val from b16 order by id;
select id, val from sibling order by id;
select id, val from pick_dst order by id;
select id, val from merge_dst order by id;
select count(*) as active_lineage_nodes
from mo_catalog.mo_branch_metadata b
join mo_catalog.mo_tables t on t.rel_id = b.table_id
where t.account_id = 0
  and t.reldatabase = 'bvt_issue_26205'
  and t.relname in (
      'b1', 'b2', 'b3', 'b4', 'b5', 'b6', 'b7', 'b8',
      'b9', 'b10', 'b11', 'b12', 'b13', 'b14', 'b15', 'b16'
  )
  and b.table_deleted = false;

drop database bvt_issue_26205;
