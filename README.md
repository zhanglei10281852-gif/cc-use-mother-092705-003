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

## 飞羽观察证据整理（`/api/birding`）

迁徙季不同入口提交同一只鸟的观察，照片说明、时间、地点和物种判断会互相矛盾。该模块把每条原始提交完整留存，按可配置规则识别可能重复的记录，并让计数由证据构成而非简单累加。

### 数据模型

- **原始观察（`bird_observations`）不可变**：完整保存观察者、时间、坐标、物种判断、可信度、照片等佐证引用和原始载荷指纹；状态只在 `active / merged / rejected` 间流转，任何决定都不删除原始行。
- **合并体（`bird_clusters`）**：代表"同一只鸟的一次在窗出现"，是计数单位。汇总全体成员的证据，保留 `primary_observation_id` 指向当前代表观察。
- **候选关系（`bird_duplicate_candidates`）**：介于两条记录之间的疑似重复，带相似度得分与打分明细，状态为 `pending / merged / kept_separate / rejected / cancelled`。
- **规则版本（`bird_rule_configs`）**：时间窗、距离半径、两个阈值、物种奖惩等全部可配，每次修改产生新版本，候选关系记录产生时的规则版本。
- **审计日志（`bird_audit_log`）**：提交、自动/人工合并、塌缩、保留独立、否决、规则变更全部留痕，可按观察或合并体回溯。

### 核心保证

1. **重试幂等**：带 `idempotency_key` 的相同请求返回同一记录（`replayed: true`），同键不同载荷返回 409；无键时同观察者+同载荷指纹也视为重试。
2. **已采用观察不被低可信后报顶替**：自动合并只归并证据；主观察只有在人工合并且可信度更高时才更换，旧主观察仍在合并体中可追溯。
3. **计数透明**：`GET /api/birding/migration-window` 返回原始提交数、活跃/合并/否决拆分、恒等校验、按物种汇总、每个合并体的成员与证据清单，以及 `bird_count` 的计算公式说明。
4. **乱序安全**：匹配按合并体全体成员取最近时间差与距离（而非只看代表观察锚点），先报"晚发生"后报"早发生"的记录也能正确归并。
5. **复核幂等**：对同一候选重复决定返回当前状态（`replayed: true`），不产生第二次副作用。

### 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/birding/observations` | 提交观察，自动识别重复（新建合并体 / 自动合并 / 进复核队列） |
| GET | `/api/birding/observations/{id}` | 原始提交详情与全链路审计 |
| GET | `/api/birding/clusters/{id}` | 合并体：成员、证据、候选、审计 |
| GET | `/api/birding/candidates` | 复核队列（`status=pending` 默认） |
| POST | `/api/birding/candidates/{id}/review` | 复核决定：`merge` 合并、`keep` 保留各自独立、`reject` 否决该记录 |
| GET | `/api/birding/migration-window` | 迁徙窗口统计：计数如何由证据组成 |
| GET/PUT | `/api/birding/rules` | 查看/更新去重规则（版本化） |
| GET | `/api/birding/audit` | 审计查询（可按观察或合并体过滤） |

