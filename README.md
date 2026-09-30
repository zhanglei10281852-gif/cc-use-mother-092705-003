# 城市生态运营服务

这是一个面向城市湿地保护团队的 Python 后端服务。项目提供本地 HTTP 接口、SQLite 持久化、身份与角色管理、审计记录、任务编排和可扩展的生态数据处理边界，便于在单机环境中保存运营状态并复核业务决定。

## 运行环境

- Python 3.11 或更高版本
- SQLite 3（使用 Python 标准库）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据文件位于 `data/compute-operations.db`，可以复制 `.env.example` 后调整本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康接口为 `GET /api/system/health`。所有状态变化都写入 SQLite，并由应用内事务保证关联记录的一致性。

## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入和现有生态计算接口。

## 编译检查

```bash
python -m compileall -q app tests
```

## 本地验收

```bash
python -m app.cli check-db
python -m app.cli smoke
```

`check-db` 检查 SQLite 完整性和外键设置，`smoke` 在进程内调用健康接口并验证基础路由。项目不依赖外部数据库、消息队列或网络服务。

## 飞羽迁徙观测证据整理

迁徙季中同一只鸟可能被不同入口重复上报，且照片说明、时间、地点互相矛盾。`app/birding` 模块提供完整的证据整理流程：

- **原始提交不可变**：`POST /api/birding/observations` 只增不改不删；每条记录保存观察者、物种判断、观测时间、地点、照片说明、佐证引用清单、载荷哈希与归属实体。
- **疑似重复识别（规则可配置）**：按时间窗口、地理距离（Haversine）、共同佐证引用、照片说明相似度加权评分；达到入队阈值生成候选，候选随附字段级冲突说明；只有近乎确定（默认共享硬佐证且 ≥0.95 分）才自动归并。
- **复核队列**：`GET /api/birding/candidates` 查看候选及双方全部证据；`POST /api/birding/candidates/{id}/decision` 可 `merge`（合并，可指定主证据）、`keep`（保留为两只）、`reject`（否决候选）。
- **可追溯**：合并只移动成员归属并归档旧实体，原始行永不删除；`GET /api/birding/observations/{id}/trace` 返回归属谱系、相关候选与审计；所有动作写入 `bird_audit`（`GET /api/birding/audit`）。
- **已采纳证据受保护**：高可信（默认 ≥0.8）或复核确认的主证据会被钉住（`pinned_winner_observation_id`），后来的低可信记录只会生成带 `challenges_adopted` 标记的待审候选，不会顶替主证据，也不会让计数下降。
- **幂等**：相同 `request_id` 重试返回同一处理结果（同键不同载荷返回 409）；完全相同的载荷即使没有 `request_id` 也自然去重；已终结候选重复提交相同决定同样幂等。
- **窗口统计**：`GET /api/birding/stats/window?start=&end=&species=&location_name=&reviewed_only=` 返回只数及 `composition`——每个目击实体的主证据（primary）、支撑证据（supporting）、观察者数、未决候选编号，以及计数口径说明；未决候选存在时计数标注为可能在复核后合并下降。
- **规则配置**：`GET/PUT /api/birding/rules` 调整时间窗口、距离、评分阈值与自动归并阈值，变更同样审计。

覆盖该模块的测试见 `tests/test_birding.py`，包含乱序到达、重复重试、互相矛盾、链式合并与低可信保护等验收场景。

