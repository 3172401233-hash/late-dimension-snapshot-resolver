# late-dimension-snapshot-resolver
Python 迟到维表快照解析库，处理版本区间、乱序事实、删除和重复修订

## 问题背景

事实记录按事件时间关联维表（如客户等级），但维表修订的到达可能晚于事实入库，
甚至同一业务时间出现多个修订。如果直接查维表当前值，历史报表会随新到修订不断
变化。本库按双时间（bitemporal）模型解析：对每条事实，选出**事件时间有效**且
**查询时已经到达**的最高版本修订；晚到的修订会生成更正（RECOMPUTE）结果。

## 数据模型

- `DimensionRevision`：维表修订，字段包括
  - `key`：业务键
  - `effective_from` / `effective_to`：生效区间，半开 `[from, to)`，`to=None` 表示开放区间；
    零长度区间（`from == to`）不匹配任何时间点
  - `arrived_at`：到达（入库）时间
  - `version`：版本号
  - `is_deleted`：是否删除（删除修订会遮蔽同区间旧版本，但自身不匹配）
  - `payload`：业务值（如客户等级）
- `Fact`：事实记录，字段为 `key` 与 `event_time`

## 生效时间与到达时间的先后关系

两个时间轴相互独立，没有固定的先后约束：

- **正常情况**：`arrived_at >= effective_from`，修订在生效前后入库，查询立即可见。
- **迟到修订**：`arrived_at` 可能远晚于 `effective_from` 甚至晚于事实的 `event_time`
  ——业务上早已生效，但系统迟迟才收到。此时历史事实在旧知识时间下的解析结果失效，
  需要重算更正。
- **预登记**：`arrived_at < effective_from` 也合法，修订提前到达、到生效时间后才参与匹配。

解析规则：对事实 `(key, event_time)` 和知识时间 `as_of`，候选修订必须同时满足
`effective_from <= event_time < effective_to`（生效轴）与 `arrived_at <= as_of`
（到达轴）；在候选中取优先级最高者，排序键为
`(version, arrived_at, effective_from, 插入序号)`——版本号最高者胜出，
版本冲突时取到达更晚的修订（视为对同版本的更正），再按生效起点与插入顺序兜底，
保证结果确定。

## 输出分类

`resolve(fact, as_of, known_as_of=None)` 返回 `Resolution`，状态三选一：

- `MATCHED`：正常匹配，`revision`/`value` 为命中的修订与取值
- `UNMATCHED`：无可见修订覆盖事件时间（含被删除遮蔽的情况）
- `RECOMPUTE`：传入 `known_as_of`（上次解析的知识时间）时，若可见胜者发生变化
  （新匹配、取值变更或被迟到删除撤回），标记为需要重算；
  `previous_revision` 保存旧结果以便比对

辅助方法：`resolve_all` 批量解析；`corrections` 只返回发生变化的更正记录；
`pending_late_revisions` 列出在 `(known_as_of, as_of]` 窗口内到达、覆盖某事件时间
的待处理迟到修订。

## 运行测试

```bash
python -m unittest
```
