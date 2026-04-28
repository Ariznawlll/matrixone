-- 验证 IN 和 NOT IN/<> 归一化
CREATE TABLE t1(a INT, b INT);
INSERT INTO t1 VALUES (1, 10), (2, 20), (3, 30), (4, 40), (5, 50);

-- IN 与 NOT IN 组合，NOT IN 过滤掉 IN 的部分
SELECT * FROM t1 WHERE a IN (1,2,3) AND a NOT IN (2);
-- IN 与 <> 组合，<> 过滤掉 IN 的部分
SELECT * FROM t1 WHERE a IN (1,2,3) AND a <> 1;
-- 多个 IN 交集
SELECT * FROM t1 WHERE a IN (1,2,3) AND a IN (2,3,4);
-- 多个 NOT IN 叠加
SELECT * FROM t1 WHERE a IN (1,2,3,4) AND a NOT IN (2) AND a NOT IN (3);
-- IN 与 IS NULL 互斥
SELECT * FROM t1 WHERE a IN (1,2) AND a IS NULL;
-- IN 与 BETWEEN 不归一化
SELECT * FROM t1 WHERE a IN (1,2,3) AND a BETWEEN 2 AND 4;
-- IN 与 NOT IN 结果为空
SELECT * FROM t1 WHERE a IN (1,2) AND a NOT IN (1,2);
-- IN 为空
SELECT * FROM t1 WHERE a IN (100);

DROP TABLE t1;