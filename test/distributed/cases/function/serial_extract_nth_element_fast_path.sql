-- 测试 serial_extract 优化：只解码目标元素
-- 创建测试表，包含 tuple 类型列
CREATE TABLE t_serial_extract (
    id INT PRIMARY KEY,
    tuple_col VARBINARY
);

-- 插入多种类型的 tuple，覆盖 int、float、bool、date、string、uuid、objectid 等
INSERT INTO t_serial_extract VALUES
    (1, serial_pack(NULL, 8, 16, 32, 64, 18, 116, 132, 164, TRUE, FALSE, 3.14, 2.718, DATE '2022-01-01', DATETIME '2022-01-01 12:34:56', TIMESTAMP '2022-01-01 12:34:56', DECIMAL64 '999', DECIMAL128 '1234567890123456789012345678901234', 'hello', UUID(), OBJECTID())),
    (2, serial_pack(400, 2024, 0xBEEF, OBJECTID(), NULL));

-- 验证 serial_extract 提取各类型元素
-- int8
SELECT id, serial_extract(tuple_col, 1, INT8) AS int8_val FROM t_serial_extract ORDER BY id;
-- int16
SELECT id, serial_extract(tuple_col, 2, INT16) AS int16_val FROM t_serial_extract ORDER BY id;
-- int32
SELECT id, serial_extract(tuple_col, 3, INT32) AS int32_val FROM t_serial_extract ORDER BY id;
-- int64
SELECT id, serial_extract(tuple_col, 4, INT64) AS int64_val FROM t_serial_extract ORDER BY id;
-- uint8
SELECT id, serial_extract(tuple_col, 5, UINT8) AS uint8_val FROM t_serial_extract ORDER BY id;
-- uint16
SELECT id, serial_extract(tuple_col, 6, UINT16) AS uint16_val FROM t_serial_extract ORDER BY id;
-- uint32
SELECT id, serial_extract(tuple_col, 7, UINT32) AS uint32_val FROM t_serial_extract ORDER BY id;
-- uint64
SELECT id, serial_extract(tuple_col, 8, UINT64) AS uint64_val FROM t_serial_extract ORDER BY id;
-- bool
SELECT id, serial_extract(tuple_col, 9, BOOL) AS bool_val FROM t_serial_extract ORDER BY id;
SELECT id, serial_extract(tuple_col, 10, BOOL) AS bool_val2 FROM t_serial_extract ORDER BY id;
-- float32
SELECT id, serial_extract(tuple_col, 11, FLOAT32) AS float32_val FROM t_serial_extract ORDER BY id;
-- float64
SELECT id, serial_extract(tuple_col, 12, FLOAT64) AS float64_val FROM t_serial_extract ORDER BY id;
-- date
SELECT id, serial_extract(tuple_col, 13, DATE) AS date_val FROM t_serial_extract ORDER BY id;
-- datetime
SELECT id, serial_extract(tuple_col, 14, DATETIME) AS datetime_val FROM t_serial_extract ORDER BY id;
-- timestamp
SELECT id, serial_extract(tuple_col, 15, TIMESTAMP) AS timestamp_val FROM t_serial_extract ORDER BY id;
-- decimal64
SELECT id, serial_extract(tuple_col, 16, DECIMAL64) AS decimal64_val FROM t_serial_extract ORDER BY id;
-- decimal128
SELECT id, serial_extract(tuple_col, 17, DECIMAL128) AS decimal128_val FROM t_serial_extract ORDER BY id;
-- varchar
SELECT id, serial_extract(tuple_col, 18, VARCHAR) AS varchar_val FROM t_serial_extract ORDER BY id;
-- uuid
SELECT id, serial_extract(tuple_col, 19, UUID) AS uuid_val FROM t_serial_extract ORDER BY id;
-- objectid
SELECT id, serial_extract(tuple_col, 20, OBJECTID) AS objectid_val FROM t_serial_extract ORDER BY id;

-- 特殊类型：time、year、bit
SELECT id, serial_extract(tuple_col, 0, TIME) AS time_val FROM t_serial_extract WHERE id=2;
SELECT id, serial_extract(tuple_col, 1, YEAR) AS year_val FROM t_serial_extract WHERE id=2;
SELECT id, serial_extract(tuple_col, 2, BIT) AS bit_val FROM t_serial_extract WHERE id=2;
SELECT id, serial_extract(tuple_col, 3, OBJECTID) AS objectid_val2 FROM t_serial_extract WHERE id=2;

-- 越界/异常：索引超范围
SELECT id, serial_extract(tuple_col, 100, INT8) AS out_of_range FROM t_serial_extract ORDER BY id;

-- 清理
DROP TABLE t_serial_extract;
