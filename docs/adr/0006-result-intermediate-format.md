# 结果抽取中间格式：Abaqus 侧只吐 CSV/JSON + manifest，平台侧入库

Abaqus 6.13 运行在 Python 2.6.2，逐单元 ODB 循环代价高。因此 Abaqus 侧抽取器只做"最笨最稳"的事：把所需场量按 **element×ply×场量** 写成 CSV/JSON，并附 **manifest**(版本、来源 ODB 哈希、集合、口径)。入库、查询与追溯放在平台侧（SQLite/Parquet），不依赖 Abaqus 内解释器。

## Consequences

- 抽取口径（section point 索引、集合、单位）必须写死在抽取器与 manifest 中，保证可复现。
- 平台与 Abaqus 之间只传文本，不传对象，天然兼容 2.6.2 与宿主语言差异。
