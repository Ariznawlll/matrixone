-- 验证 serial_extract 针对 tuple 类型列的提取性能与正确性，覆盖多种类型
CREATE TABLE t_tuple (
    id INT PRIMARY KEY,
    tup VARBINARY
);

-- 插入包含多种类型的 tuple 数据
INSERT INTO t_tuple VALUES
  (1, serial((123, 456.789, true, 'abc', date '2024-06-01'))),
  (2, serial((NULL, -42, false, 'xyz', date '2020-01-01'))),
  (3, serial((999, 0.001, true, 'hello', date '1999-12-31')));

-- 提取第 0 个元素（int 或 null）
SELECT id, serial_extract(tup, 0, INT) AS c0 FROM t_tuple ORDER BY id;
-- 提取第 1 个元素（float）
SELECT id, serial_extract(tup, 1, FLOAT) AS c1 FROM t_tuple ORDER BY id;
-- 提取第 2 个元素（bool）
SELECT id, serial_extract(tup, 2, BOOL) AS c2 FROM t_tuple ORDER BY id;
-- 提取第 3 个元素（varchar）
SELECT id, serial_extract(tup, 3, VARCHAR) AS c3 FROM t_tuple ORDER BY id;
-- 提取第 4 个元素（date）
SELECT id, serial_extract(tup, 4, DATE) AS c4 FROM t_tuple ORDER BY id;

-- 提取超出范围的元素，期望报错或返回 null
-- @regex("index out of range", true)
SELECT serial_extract(tup, 5, INT) FROM t_tuple;

DROP TABLE t_tuple;
