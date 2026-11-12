# 玩家 ReplayInfo 局内行为分析与初始牌面复现交接手册

> 适用对象：接手 Tile 类关卡玩家局内行为分析的 AI、数据分析师或工具开发者。
>
> 核心目标：仅依赖玩家对局数据、对应版本的关卡模板与配置快照，复现玩家进入关卡时的 `TileNum → 空间位置 → 最终图案`，再按真实操作顺序推进局面并提取实时行为特征。
>
> 当前代码基线：`/Users/你的用户名/Projects/hhtools/玩家行为分析工具`。
>
> 当前模板目录：`/Users/你的用户名/Projects/tile/Tile/Assets/Resources/TileMatchLevelData`。
>
> 当前配置目录：`/Users/你的用户名/Projects/ulinkconfig/csv`。

---

## 0. 先记住这几个最重要的结论

1. `replay_info` 里的时间不是局内累计时间，而是“距上一次记录操作的间隔毫秒数”；局内时间必须从头累加。单段最多记 `120000ms`，出现该值后，累计时间只能解释为下界。
2. `TileNum` 不是坐标，也不是图案 ID。它是模板中 `LevelData/TileData` 的序列号，1 基。必须先解析模板并保持序列化顺序，才能把 `TileNum` 映射到 `(x,y,z)`。
3. `DupParaN` 的四段含义固定为：`A图案种类数 | 随机种子 | 塔幸运值 | B图案种类数`。第二段才是初始图案随机分配的种子；第三段不参与初始 A/B 图案落位。
4. “随机种子”不等于 Python 的 `random.seed`。客户端使用自定义 uint32 随机数发生器，种子先加固定常数 `1411242022`，每一步还要按 C# 无符号 32 位溢出计算。
5. 图案分配有两条算法：`randomSeed_groupID` 缺失或为 0 时走旧版“均分＋三轮洗牌”；大于 0 时走结构化分配。不可混用。
6. 严格的实际关卡实例键应为：

   ```text
   level_index
   + level_template_id
   + level_dup_para_id
   + canonical(level_dup_para)
   ```

   `level_id`、版本、平台和用户分组用于血缘、配置校验和稳健性分析；一旦有效模板和 DupPara 已确定，DDA 只解释“为什么这个玩家拿到这一实例”，不再进入内容身份。
7. 初始牌面复现不等于完整局内模拟。塔内动态换牌、洗牌/移动类道具、复活、部分运行时替换会改变后续状态；没有实现相应 Handler 时，只能把这些动作之前的状态当作精确状态，之后要降级置信度。
8. 空 `replay_info` 只跳过该行，不应阻塞同一批其他有效对局。

---

## 1. 分析目标与正确的分层思路

这项工作的重点不是用模板层数、总牌数等静态特征直接给关卡定难度，而是回答：

- 同一玩家玩不同关卡时，在哪些关卡明显偏离他自己的正常行为？
- 这种偏离是否被其他玩家稳定复刻？
- 偏离发生在局内什么时刻、什么局面、什么选择之后？
- 玩家看到的可见信息为什么会让路径变窄、背包变满或思考变长？

推荐的证据链是：

```text
模板与参数
→ 初始空间结构和图案分布
→ 每次点击前玩家实际面对的局面
→ 玩家选择、停顿、道具和恢复行为
→ 与该玩家其它关卡的个人基线比较
→ 检查其他玩家是否复刻同方向差异
→ 形成稳定的关卡过程标签
```

失败率、人均消耗和流失是结果校验，不直接决定过程分层。一个关卡可能“高压力、低消耗、高失败流失”，这与“高消耗、高压力”是两类不同问题。

---

## 2. 必需数据与推荐数据

### 2.1 最小可用字段

至少需要：

| 字段 | 用途 |
|---|---|
| `user_id` | 建立玩家内基线、跨关比较 |
| `event_time` | 对局时间和配置版本核验 |
| `client_version` | 选择对应代码/配置语义 |
| `platform` | Android/iOS 资产配置和版本差异 |
| `level_index` | 进度位置、候选图案池选择 |
| `level_template_id` | 找到模板 `<id>_m.bytes` |
| `replay_info` | 操作序列、间隔、动作和目标 TileNum |

### 2.2 强烈建议同时保留

| 字段 | 为什么重要 |
|---|---|
| `level_id` | 精确关联 `config_level.csv`，避免按名称猜行 |
| `level_dup_para_id` | 确定读取 `DupPara1…DupPara10` 中哪一列 |
| `level_dup_para` | 当局实际下发参数；历史分析时优先级高于当前仓库配置 |
| `level_play_count` | 首局与非首局分开分析，不等于丢弃重试 |
| `level_status` / `result_category` | 区分通关、背包满失败、正常失败等 |
| `bag_tile_change_curve` | 校验背包推进、塔内换牌和道具影响 |
| `surface_normal_tile_change_curve` | 校验表面牌数量及塔顶变化 |
| `bag_full_event_count` | 判断背包满事件和容量证据 |
| `level_revive_count` | 复活上下文 |
| `level_space_revive_count` | 背包扩容复活上下文 |
| `level_time_revive_count` | 时间复活上下文 |
| 背包容量/扩容来源字段 | 区分 7 格、8 格、自动权益和玩家主动道具 |

### 2.3 样本选择

若目标是给一段连续关卡分层，优先选择已经到达该区间之后较远位置的同一批玩家，再回溯他们在目标区间内的全部正常结算对局。例如研究 100–199 关，可选择已到达 300 关以上的玩家。这样能提高区间覆盖，但仍应：

- 保留首局和非首局，分别聚合；重试拿到的新参数/新图案是新的实际实例。
- 多批 Redash 导出保持产品、平台、版本范围、日期、关卡区间和到达门槛一致，只改变稳定哈希排名区间。
- 合并文件时按 UID 去重或验证互斥；不要按文件名推断批次互斥。
- 空回放、异常退出或无法配对的数据进入质量统计，但不阻断其余样本。

---

## 3. `replay_info` 的准确语义

### 3.1 文法

```text
interval_ms#action_id[:tile_num1-tile_num2-...]
```

多步以 `|` 分隔：

```text
0#2:73-75-79|5643#708|1098#1:78|366#1:71|1612#1:98
```

解释：

- `0#2:73-75-79`：进关时立即应用局前 Booster，影响 TileNum 73、75、79。
- `5643#708`：距上一步 5.643 秒发生 708 动作，无目标 Tile。
- `1098#1:78`：再过 1.098 秒，玩家点击 TileNum 78。
- `366#1:71`：再过 0.366 秒，点击 TileNum 71。
- `1612#1:98`：再过 1.612 秒，点击 TileNum 98。

累计后前三次点击发生在 6.741、7.107、8.719 秒。

### 3.2 动作类型

| `action_id` | 含义 |
|---|---|
| `1` | 玩家点击牌 |
| `2` | 局前 Booster 兼容类型 |
| 其他数字 | 道具 `AssetId/propType` |

道具后的 TileNum 是该动作产生的 `MoveAction` 涉及牌，不一定是玩家手动选中的牌。动作名应查对应平台的：

- `config_server_asset.csv`
- `config_client_asset_show.csv`

当前已知扩容类兼容 ID：

- `508`：普通局内背包 +1，通常视为玩家主动道具。
- `708`：限时权益自动应用，不应记作玩家主动决策。
- `601/801`：历史/兼容映射，需按版本配置解释。
- `533`：旧版活动扩容可能出现；2.08 的回放链路可能不再记录它，因此“没有 `#533`”不能证明容量一定是 7 格。

### 3.3 时间处理

每一步：

```text
elapsed_ms[i] = Σ interval_ms[0..i]
```

不要把自动 708 的间隔合并到下一次点击。每个动作自己的 `interval_ms` 就是该动作前的等待时长；对玩家决策时长做分析时，自动动作本身不作为玩家决策样本。

客户端把任何 `interval >= 120000` 记录成 `120000`。所以首次出现 120000 后：

- 当前及之后的累计时间是下界；
- 可以计算“至少等待 120 秒”的右删失指标；
- 不可把 120 秒当作精确等待时长。

### 3.4 推荐解析结构

每步至少输出：

```text
step_no
raw_step
interval_ms
elapsed_ms
elapsed_is_lower_bound
action_id
action_name
action_origin
is_player_decision
tile_nums[]
valid
parse_error
```

正则参考：

```regex
^(?<interval_ms>\d+)#(?<action_id>\d+)(?::(?<tile_nums>\d+(?:-\d+)*))?$
```

---

## 4. 什么才是“一关”

`level_index=150` 只表示进度位置，不代表玩家拿到完全相同的内容。模板只定义空间骨架；副本参数和随机种子决定 A/B 图案种类与落位；DDA 负责选择下发哪个实例。

推荐业务实例键：

```python
instance_key = (
    int(level_index),
    str(level_template_id),
    int(level_dup_para_id),
    canonical_dup_para(level_dup_para),
)
```

其中 `canonical_dup_para` 应把四段都转成整数后重新用 `|` 连接，避免空格或字符串格式造成伪重复。

为什么不把 `level_id` 强行放入实例键：不同血缘行可能最终指向同一模板和同一有效参数；内容分析应避免把同内容拆成两关。但必须保留 `level_id` 用于：

- 精确查配置；
- 识别历史路由或价值测试；
- 检查不同版本是否发生同键异义；
- 审计配置来源。

为什么不把 DDA 分组放入实例键：DDA 是分配机制。只要当局模板、副本号和实际 DupPara 已确定，局内初始内容已经确定。`U1_0` 是用户分组，不是“这局难度档位”。

---

## 5. 从关卡号定位模板与参数

### 5.1 涉及的配置表

| 文件 | 作用 |
|---|---|
| `config_level_list.csv` | `level_index → LevelId / DDALevelId1 / 2 / 3` 路由 |
| `config_level.csv` | `LevelId → TemplateId / DupPara1…10 / randomSeed_groupID` |
| `config_level_value_test.csv` | 价值测试关卡的特殊映射 |
| `config_client_level_tile.csv` | 不同关卡段的候选图案优先池 `TileList1/TileList2` |
| `config_client_tile.csv` | TileId 是否存在，以及图案描述/图标映射 |
| `config_level_randomSeed_group.csv` | 结构化随机分配组：副本号 → seed 配置 ID |
| `config_level_randomSeed.csv` | 结构化分配参数：卡点数、层间距、阶段分组等 |

### 5.2 配置行解析优先级

按以下顺序，越靠前证据越强：

1. 输入数据自带的实际 `level_id`，且其配置行的模板与输入 `level_template_id` 一致。
2. 明确标记的价值测试映射，且唯一。
3. `config_level_list.csv` 中当前 LevelId/DDA LevelId，筛选模板后一行唯一。
4. 未明确否定价值测试时，可唯一推断出的价值测试行。
5. 历史命名兜底，例如 `list{level_index}r3v1{template_id}`。
6. 如果输入已经带实际 `level_dup_para`，即使当前配置找不到历史 `level_id`，仍可用输入参数复现旧版分配。
7. 只有当同模板所有候选配置行在目标 `DupParaN` 和 `randomSeed_groupID` 上都相同，才允许模板级唯一签名兜底。
8. 其余情况标记“配置无法唯一确定”，不要挑第一行、不要静默猜测。

历史对局最可靠的是 SQL 直接输出当局 `level_id` 与 `level_dup_para`。当前仓库配置只能证明“按当前配置可推断”，不能自动升级成“历史实际下发”。

### 5.3 `DupParaN` 的四段

客户端 `LevelDupParameter.cs` 明确解析为：

```text
TileTypeCountA | RandomSeed | TowerLucky | TileTypeCountB
```

例如：

```text
11|3|100|7
```

表示：

- A 区最多使用 11 种候选图案；
- 图案选择和分配种子为 3；
- 塔幸运值为 100；
- B 区最多使用 7 种候选图案。

注意：

- `TowerLucky` 影响塔的运行时帮助/换牌行为，不改变初始 A/B 赋图结果。
- IAP 保护可能动态减少 A 种类数：`effective_A = max(1, A - iap_cut_a)`。这依赖玩家最近付费时间与版本配置；无法取得时必须记录假设。当前 2.08 Android 历史分析采用 `iap_cut_a=0`。

---

## 6. 解析模板并建立 TileNum 映射

### 6.1 模板文件

文件名：

```text
<level_template_id>_m.bytes
```

它是 protobuf 二进制。当前已确认的 `LevelSaveData` 顶层字段：

| 字段号 | 含义 |
|---|---|
| 1 | Version |
| 2 | TemplateId |
| 3 | DuplicateId，已废弃 |
| 4 | LevelWidth |
| 5 | LevelHeight |
| 6 | repeated TileData |
| 7 | repeated StackData/TowerData |
| 8 | repeated DebuffData |

`TileData`：

| 字段号 | 含义 |
|---|---|
| 1 | X |
| 2 | Y |
| 3 | Z；为 0 时 protobuf 可能省略 |
| 4 | TileId |
| 5 | DebuffDataIndex |

`StackData`：X、Y、Direction；Direction=0 时也可能省略。

### 6.2 TileNum 的来源

解析顶层字段时，按文件中 field 6 的出现顺序得到 `tiles[0], tiles[1]...`：

```text
TileNum = tile_index + 1
```

不要在读取后自行按坐标重排。虽然当前编辑器构造数据时通常按 `Z → Y → X` 排序，但回放记录对应的是最终序列化 `LevelData` 顺序；擅自重排会把点击映射到错误牌。

### 6.3 坐标和遮挡

- 左上角为原点。
- X 水平，Y 垂直，Z 从底向上。
- 一个普通 Tile 占四个整数网格单元：

  ```text
  (x,y), (x+1,y), (x,y+1), (x+1,y+1)
  ```

- 高 Z Tile 与低 Z Tile 投影有 1、2 或 4 格重叠时，就遮挡对应的 1/4、1/2 或全部区域。
- “至少露出 1/4”定义为可见；四个 1/4 都没有未移除的上层遮挡，才定义为可点击。

推荐预计算：

```text
quarter_blockers[tile_index][0..3]
direct_blockers[tile_index]
blocked_tiles[tile_index]
```

状态只需维护 `removed_tile_indices`，即可快速计算任意一步的可见和可点击集合。

### 6.4 塔牌识别

运行时规则不是“附近的牌都属于塔”，而是：Tile 的 `(x,y)` 与某个 StackData 锚点完全相等时，该 Tile 属于该塔。若出现重复锚点，以最后一个为准。塔内牌初始几何成员可还原，但塔触发后的图案换色和顶牌选择可能依赖运行时逻辑。

### 6.5 模板占位 TileId

```text
A = -10
B = -11
C = -12
...
N = -23
```

- A/B 走配置参数和确定性随机分配。
- C–N 是结构组占位。旧逻辑可能使用 `System.Random()` 给各组选择普通图案，不能只靠 DupPara 种子唯一复原具体图案。
- 即便 C–N 图案未知，坐标、层级、遮挡、可见/可点击和时间指标仍可分析；只降低依赖具体花色的指标置信度。

---

## 7. 生成候选图案池

旧版和结构化算法都先调用同一逻辑生成候选图案列表。

### 7.1 选关卡段配置

在 `config_client_level_tile.csv` 中选择：

```text
StartLevel <= level_index
```

且 `StartLevel` 最大的那一行，读取 `TileList1`、`TileList2`，保持 CSV 中原始顺序。

### 7.2 需要多少种

```text
need = max(1, effective_A_count, B_count)
```

### 7.3 固定基础图案池

若不能直接从客户端运行时取 `GetNormalBaseTileModels()`，当前代码验证过的基础顺序为：

```text
1000,1001,1002,1003,1004,1005,1006,1007,1008,1009,
1011,1012,1013,1014,1300,1301,1302,1304,1313
```

1010 是 Golden Shell，在结构化卡点图案选择中会排除。候选 TileId 还应在 `config_client_tile.csv` 中存在；无资源的 ID 不加入结果。

### 7.4 候选池算法

```python
rng = CustomRandom(game_seed)
selected = []

while len(selected) < need and list1:
    id = random_pop(list1, rng)
    remove_same_id_from(list2)
    remove_same_id_from(base_pool)
    if id_exists_in_config_client_tile:
        selected.append(id)

while len(selected) < need and list2:
    id = random_pop(list2, rng)
    remove_same_id_from(base_pool)
    if id_exists_in_config_client_tile:
        selected.append(id)

while len(selected) < need and base_pool:
    selected.append(random_pop(base_pool, rng))
```

这里的顺序是算法的一部分。不能先转 `set`、不能排序、不能用 Python 随机库替换。

---

## 8. 精确复刻客户端 `CustomRandom`

### 8.1 最终种子

```text
game_seed = uint32(1411242022 + DupPara.RandomSeed)
```

### 8.2 随机数发生器

```python
MASK = 0xffffffff

class CustomRandom:
    def __init__(self, seed):
        self.mw = seed & MASK
        self.mz = 362436069

    def get_random(self, maximum):
        self.mz = (36969 * (self.mz & 65535) + (self.mz >> 16)) & MASK
        self.mw = (18000 * (self.mw & 65535) + (self.mw >> 16)) & MASK
        combined = (((self.mz << 16) & MASK) + self.mw) & MASK
        return combined % maximum
```

必须保留每一步 uint32 溢出。Python 整数不会自动溢出，所以每步都要 `& 0xffffffff`。

### 8.3 已有回归向量

可用以下结果验证候选图案池实现：

```text
level_index=301, seed=1, A=11, B=7
→ [1013,1300,1007,1008,1005,1014,1313,1001,1004,1011,1009]

level_index=310, seed=3
→ [1013,1001,1012,1004,1302,1009,1304,1301,1005,1300,1006]
```

若不一致，先排查：配置快照、TileList 顺序、基础图案顺序、uint32 溢出、随机调用次数。

---

## 9. 旧版随机分配：均分＋三轮交换洗牌

判定条件：`config_level.csv` 对应行的 `randomSeed_groupID` 为空或 `<=0`。

### 9.1 A/B 独立执行

对 A 和 B 分别取模板中 `TileId=-10/-11` 的目标牌，按原始模板顺序赋值。两组都使用同一个候选列表和同一个 DupPara 种子，但每一组洗牌时都重新初始化 `CustomRandom(game_seed)`。

### 9.2 图案及三消组配额

对某组：

```text
tile_count = 该组占位牌数量
group_count = floor(tile_count / 3)
types = candidates[:tile_type_count]
types 按权重降序、再按 TileId 降序
```

当前所有权重都是 100，所以实际是 TileId 降序。

每种图案的基础组数：

```text
floor(float32(float32(100) / float32(total_weight))
      * float32(group_count))
```

规则：

1. 只要总组数还有空间，每种图案至少给 1 组。
2. 不得让累计组数超过 `group_count`。
3. 剩余组数按排序后的前几个图案依次补 1 组。
4. 每组展开成 3 个相同 TileId。

注意 Unity/C# 这里走单精度 float。若直接用 Python 双精度，边界样本可能多一组或少一组。应使用 `struct.pack/unpack` 模拟 float32。

### 9.3 三轮洗牌

这不是 Fisher–Yates：

```python
rng = CustomRandom(game_seed)
for _ in range(3):
    for i in range(len(result)):
        j = rng.get_random(len(result))
        result[i], result[j] = result[j], result[i]
```

最后按模板中 A/B 占位牌出现顺序逐个写入。

### 9.4 旧版伪代码

```python
def assign_legacy(template_tiles, candidates, a_count, b_count, seed):
    for placeholder, type_count in [(-10, a_count), (-11, b_count)]:
        targets = [t for t in template_tiles if t.source_tile_id == placeholder]
        patterns = make_balanced_triplets(
            candidates=candidates,
            type_count=type_count,
            tile_count=len(targets),
            unity_float32=True,
        )
        shuffle_three_passes(patterns, CustomRandom(1411242022 + seed))
        for tile, pattern in zip(targets, patterns):
            tile.initial_tile_id = pattern
```

### 9.5 异常检查

- A/B 占位牌数量原则上应能被 3 整除。
- 分配后每个具体图案数量也应能被 3 整除。
- 生成列表长度小于占位牌数量时，不要部分填充后假装成功，应标记配置/模板异常。

---

## 10. 结构化随机分配

判定条件：`randomSeed_groupID > 0`。

### 10.1 读取结构化配置

1. 用 `randomSeed_groupID` 查 `config_level_randomSeed_group.csv`。
2. 按 `level_dup_para_id` 读取该行 `DupParaN`，得到 seed config ID。
3. 用该 ID 查 `config_level_randomSeed.csv`，读取：

| 字段 | 含义 |
|---|---|
| `ChokeTile_num` | 卡点图案种类数 |
| `ChokeTile_startLayer` | 顶部排除/卡点可开始层约束 |
| `ChokeTile_param` | 卡点图案层间距要求 |
| `NormalTile_param` | 普通图案层间距/兼容参数；以目标版本源码为准 |
| `Stage_param` | 非卡点图案在阶段间的比例，例如 `40|30|30` |
| `TileKind_num` | 各阶段普通图案优先占用的连续层数 |

### 10.2 共享分类、分组落位

结构化分配使用一次持续推进的 `CustomRandom`，不要像旧版 A/B 洗牌那样每组重新设种子。

1. 从 `candidates[:max(A_count,B_count)]` 中随机选 `ChokeTile_num` 个卡点图案，排除 1010。
2. 剩余候选为非卡点图案。
3. A 可用“卡点＋非卡点”；B 只用非卡点。
4. 每组先拆为：
   - 阵型牌：非塔占位牌，按 `(z,y,x)` 排序。
   - 塔牌：属于 StackData 锚点的占位牌。
5. 以“阵型牌数＋塔牌数”为总量，按三消组给所有图案分配能被 3 整除的总配额。

### 10.3 卡点图案

对 A 中每个卡点图案：

- 根据 `ChokeTile_startLayer` 排除顶部若干层；
- 尝试把同图案配额分到上下两段层区间；
- 两段之间满足 `ChokeTile_param` 的层距；
- 若容量不足，依次放宽顶部排除、层距，最后退化到可用层分配；
- 每次随机挑选上下分割位置和各层分配时，都必须消耗同一 RNG 流。

### 10.4 非卡点图案

1. 按 `Stage_param` 把非卡点图案列表分组；中间组采用整数四舍五入，最后一组接收余数。
2. 根据当前阵型剩余容量与塔数量，拆分每个图案的“阵型配额”和“塔配额”。
3. 塔牌先用 Fisher–Yates 洗牌，再依配额填充。
4. 阵型部分按 `TileKind_num` 指定的连续层数，从底部可用层开始为一种图案连续安排。
5. 阵型容量放不下的溢出牌回填到塔。
6. 各层槽位内部再用同一 RNG 做 Fisher–Yates。
7. 最后修复未分配牌及不能被 3 整除的余数。

### 10.5 为什么不能只照旧版洗牌

结构化算法会主动制造同花色跨层间隔、阶段种类变化和塔内配额，其目标正是控制卡点结构。若仍用旧版均分洗牌，虽然每种图案总数可能正确，但玩家看到的层间分布和实时可解路径会完全不同，后续行为分析没有意义。

### 10.6 权威源码与当前复刻实现

结构化分配权威参考位于历史发布分支：

```bash
git -C /Users/你的用户名/Projects/tile show \
  remotes/origin/tile_match_a-v2.08.0:ulink/TileEnv/TileMatch/Data/LevelGenerator/RandomSeed/RandomSeedAllocator.cs
```

本地复刻函数位于：

```text
scripts/analysis/restore_player_replay.py
```

重点函数：

```text
classify_patterns
plan_choke_layers
plan_consecutive_layers
pattern_quotas
group_patterns_by_stage
assign_layer_slots
ensure_assigned_and_divisible
allocate_structured_group
```

跨版本接手时，不要只相信函数名或旧文档；先用事件对应的客户端分支重新对照随机调用顺序。

---

## 11. 生成初始牌面映射表

完成模板解析和 A/B 分配后，每张牌至少输出：

```text
level_index
level_template_id
resolved_level_id
level_dup_para_id
resolved_dup_para
config_mapping_source
random_algorithm
reconstruction_status
tile_num
initial_x
initial_y
initial_z
initial_layer
tower_index
source_tile_id
initial_tile_id_after_random
tile_name
tile_icon
is_choke_pattern
```

推荐给每个实例缓存一次映射：

```text
cache_key = (
  level_index,
  level_template_id,
  level_dup_para_id,
  canonical_dup_para,
  config_snapshot_hash,
  template_sha256,
  iap_cut_a,
)
```

不要把以下两类结果混成同一状态：

- `已还原_使用输入实际配置`：输入带实际 `level_dup_para`，证据强。
- `已还原_使用当前配置推断`：依据当前仓库配置推断历史对局，可能受配置变更影响。

若含 C–N，可写：`结构可信_C-N具体图案未知`；几何、时间和不依赖花色的指标继续计算。

---

## 12. 如何验证初始图案复现正确

### 12.1 局前 Booster 是最强交叉证据

若回放包含：

```text
0#2:73-75-79
```

并且该 Booster 的语义是选出一组三消目标，则复原结果中 73、75、79 应映射到同一最终 TileId。这比“看起来合理”强得多。

现有历史验证：

- 1,100 局中，1,065 局走旧版分配，34 局走结构化分配，1 局因配置歧义不猜。
- 832 局带目标牌的局前 Booster 中，831 局可验证；831/831 的每组 3 张都映射为同图案。
- 其中覆盖 29 局结构化分配样本。

这证明当前版本实现具备很强一致性，但不能自动证明未来版本仍使用相同算法。

### 12.2 逐层质量检查

每局至少记录：

- 模板是否存在、能否解析；
- TileNum 是否越界；
- A/B 数量是否能被 3 整除；
- 最终图案数量是否满足三消整除；
- 配置行是实际命中、唯一推断还是歧义；
- Booster 三张一致率；
- 点击 TileNum 是否在当时几何上可点击；
- 模拟背包曲线与埋点曲线的一致率；
- 模拟表面牌数量与 `surface_normal_tile_change_curve` 的一致率；
- 塔、C–N、道具、复活导致的降级原因。

### 12.3 出现不一致时的排查顺序

1. 是否用了事件当天/客户端版本对应的配置快照。
2. SQL 是否带了实际 `level_id`、`level_dup_para_id`、`level_dup_para`。
3. 是否保留模板序列化顺序，而非重排 TileNum。
4. 是否选了正确的 `config_client_level_tile` 行。
5. TileList 和基础图案池是否保留原顺序。
6. `CustomRandom` 是否模拟 uint32 溢出。
7. 旧版配额是否模拟 Unity float32。
8. 旧版是否执行三轮“全长随机交换”而不是 Fisher–Yates。
9. 是否错误地把结构化关当成旧版。
10. IAP 保护是否让 A 种类数减少。
11. 是否存在 C–N、塔内动态换牌或移动/洗牌道具。

---

## 13. 从初始牌面推进局面

### 13.1 基础状态

```python
state = {
    "removed": set(),
    "bag_counts": Counter(),
    "bag_capacity_min": 7,
    "bag_capacity_max": 8,
    "current_tile_ids": initial_tile_ids,
    "elapsed_ms": 0,
    "confidence": "exact_or_bounded",
}
```

### 13.2 点击转移

对 `#1:TileNum`：

1. 将 TileNum 转 0 基 index。
2. 在点击前计算可见、可点击、背包、路径和信息指标。
3. 检查该牌是否在可点击集合；不在则标记映射/动态状态异常，不要自动删除遮挡牌来“修正”。
4. 将该牌图案加入背包。
5. 同图案数量达到 3 时清除这 3 张；否则保留。
6. 标记该 Tile 已移除。
7. 重新计算新露出、新可点击、表面积变化。
8. 用实际 bag/surface 曲线校验；冲突时保留实测曲线，降低图案点估计置信度。

基础转移：

```python
bag[pattern] += 1
if bag[pattern] == 3:
    del bag[pattern]
removed.add(tile_index)
```

### 13.3 背包容量上下界

当前埋点并不总能精确区分 7/8 格，推荐输出上下界：

- 首次点击前记录扩容动作：`8/8`。
- 曲线明确观察到达到 8 格：`8/8`。
- 最大 7 格且发生背包满失败：`7/7`。
- 其余：`7/8`，因为可能存在未写入 replay 的旧活动扩容。
- 中途扩容动作发生后，上下界都切为 8。

不要仅凭“没有 708”判为 7 格，也不要把 708 当作玩家主动使用道具。

### 13.4 塔

- 塔动态换牌通常不改变几何位置集合，因此可见/可点击几何仍能推进。
- 图案身份可能变化。可结合点击后的 surface 曲线反推实际塔顶位置，但塔触发后花色通常只是点估计。
- 背包实际变化与预测不一致时，可按“实际背包大小增量”进行最小校准，但必须保留 `pattern_confidence`。

### 13.5 道具和复活

移动、抓钩、洗牌、撤回、复活等动作若未实现对应 Handler：

- 动作前状态仍可精确分析。
- 动作之后的几何或图案状态降级为不确定。
- 继续使用 `bag_tile_change_curve`、`surface_normal_tile_change_curve` 计算观测指标。
- 不要用静态模板强行推演一个虚假的精确后续状态。

如需真正模拟道具效果，入口映射在游戏代码 `PropClickHandlerFactory`，各 Handler 位于：

```text
/Users/你的用户名/Projects/tile/ulink/TileEnv/TileMatch/GamePlayer/Data/Handlers/Prop/
```

---

## 14. 每次决策前应计算的实时局面指标

以下是行为分析的核心。不要只统计总耗时和最终失败。

### 14.1 节奏

- 当前动作自己的 `interval_ms/1000`。
- 点击思考时长中位数、P90。
- `>=3s`、`>=5s`、`>=10s` 停顿占比。
- 前/中/后段点击时长与后段减速。
- 可见无解状态下思考时长，与正常有解状态之比。
- 120 秒右删失次数。

### 14.2 背包压力

- 点击前背包占用、容量上下界、剩余格数。
- 高压状态占比：例如 `bag_size >= capacity-2`。
- 压力连续段数量与最长持续点击数。
- 高压后是否恢复到安全区、恢复所需点击数。
- 压力与可见无解同时出现的比例。
- 背包满前最后 N 步的选择与停顿。

### 14.3 表面积和信息暴露

- 当前可点击牌数量。
- 至少露出 1/4 的可见牌数量。
- 仅部分可见但不可点击的牌占比。
- 当前可见 1/4 总数。
- 当前可见图案种类、可点击图案种类。
- 点击后新增可见牌数、新增可点击牌数和 1/4 暴露增量。

### 14.4 可见路径宽度

设背包中某花色已有 `b` 张，则再需要 `need=max(0,3-b)` 张。计算：

- 可见集合中能补齐三消的花色数。
- 在剩余容量内能安全补齐的花色数。
- 可直接完成三消的可点击动作数。
- `visible_no_solution`：背包＋当前至少露出 1/4 的牌中，没有任何花色能凑到 3。
- `visible_narrow`：可见可解花色数不超过 1。

“可见无解”只描述玩家当前信息集，不等于全局数学无解。

### 14.5 高压短窗可存活首步

当背包已高压时，枚举当前合法首步并做有限深度搜索：在背包装满前，能否先完成一次三消释放空间。

分别做两种搜索：

- 可见短窗：后续只允许使用当前已经可见的牌，近似玩家信息。
- 全知短窗：允许点击后新揭露的隐藏牌，代表局面客观上是否存在短期路线。

输出：

- 可见可存活首步数。
- 全知可存活首步数。
- 可见首步数为 0：当前玩家视角被迫/无路。
- 可见为 0、全知大于 0：隐藏信息依赖。
- 玩家选择是否属于可存活首步。
- 存在安全替代但玩家选了不可行步：选择陷阱候选。

### 14.6 选择质量

- 是否优先完成即时三消。
- 是否选择能释放更多下层牌的 Tile。
- 是否从背包已有 1/2 张的花色中继续补齐。
- 是否在存在安全路线时选入短窗不可行路径。
- 一次点击后可行路径宽度变大还是变小。

### 14.7 道具行为

- 主动道具每 100 次点击使用数。
- 道具发生时的背包占用、可见无解、路径宽度、思考时长。
- 使用后 N 步内是否恢复、是否形成三消、是否通关。
- 自动 708 单独作为环境/权益上下文，不计入主动道具依赖。

---

## 15. 从单局指标到关卡分层

### 15.1 不要直接跨玩家比绝对值

快手玩家和慢手玩家的绝对点击间隔不同。正确做法是先在同一玩家内部比较：

1. 按业务实例和首局/非首局阶段聚合，避免某玩家重试多次而过度加权。
2. 对每个玩家、每个阶段，用该玩家其它关卡的指标中位数作为基线。
3. 当前关卡从基线的偏离用稳健尺度标准化。

推荐：

```text
center = median(other_levels)
scale = 1.4826 * MAD(other_levels)
player_z = (current_transformed - center) / scale
```

若 MAD 为 0，可依次回退到 IQR/1.349、标准差或该阶段全局稳健尺度。比例先 logit，计数/时间先 `log1p`，减少长尾影响。

同时可输出 ±5 关局部基线，但主结论不应只依赖相邻关，因为整段可能连续上升。

### 15.2 检查跨玩家复刻

对同一实例、同一阶段、同一指标的 `player_z`：

- 中位效应绝对值建议至少 0.5。
- 方向一致率建议至少 70%。
- 探索信号：至少 5 名玩家。
- 正式复刻：至少 10 名玩家，并要求 Wilson 90% 下界 > 0.5、bootstrap 90% 区间不跨 0、随机对半方向一致率至少 80%。
- 高置信复刻：至少 20 名玩家，对半方向一致率至少 90%。
- 同时做 leave-one-player-out，检查结论是否被单个玩家决定。

这些门槛不是把关卡强行等分。若 100 关中 99 关都难，方法允许 99 关都落在高负担区域；不可用四分位数硬性制造“25% 简单、25% 困难”。

### 15.3 当前建议的主分层轴

主分层只使用实时行为：

1. 节奏卡顿：每点击耗时相对个人基线升高。
2. 背包压力：确定高压状态占比升高。
3. 路径压缩：可见窄路、可见无解、短窗无存活路径升高。
4. 认知停滞：可见无解时思考明显变长。
5. 选择陷阱：存在安全替代时仍稳定诱发不可行选择。

过程标签：

| 标签 | 含义 |
|---|---|
| `L4_多维高负担` | 至少 3 个主轴稳定高负担 |
| `L3_复合卡点` | 2 个主轴稳定高负担 |
| `L2_单点卡顿` | 1 个主轴稳定高负担 |
| `L1_稳定低负担` | 无高负担轴，至少 1 个轴稳定低负担 |
| `L0_未见稳定过程差异` | 样本足够，但未检出跨玩家一致异常 |
| `P_待补样` | 实例阶段有效玩家少于 10，不强行定层 |

失败、人均消耗、流失、模板静态特征用于解释和校验，不直接决定 L0–L4。

---

## 16. 精确性分层：不要把所有行都当成同等可信

建议每局、每一步分别记录置信度，而不是整局二选一：

### A. 精确或强证据

- 输入含实际 `level_id` 与实际 `level_dup_para`。
- 模板和配置快照与事件版本一致。
- A/B 随机算法已匹配。
- TileNum 全部合法。
- Booster、背包、surface 曲线验证一致。
- 没有未实现的动态道具影响当前时点。

### B. 高置信推断

- 缺少实际参数，但当前路由与模板能唯一确定配置。
- 已通过局前 Booster 等证据交叉验证。
- 仍需明确标为“当前配置推断”。

### C. 部分可用

- C–N 具体图案未知，但几何和时间可信。
- 塔触发后图案不确定，但几何仍可推进。
- 移动/洗牌道具后只保留观测曲线和时间指标。
- 7/8 格容量只能给上下界。

### D. 不可还原

- 模板缺失/损坏。
- 配置行有歧义且输入无实际 DupPara。
- TileNum 大量越界。
- 版本和配置来源完全无法核验。

原则：只降级受影响的维度。图案未知不代表时间不能分析；动态换牌不代表模板几何完全失效；一行空回放不代表整批都不能做。

---

## 17. 推荐的端到端工程流程

### 阶段 1：输入审计

1. 读取一个或多个 CSV，自动识别 UTF-8-SIG/UTF-8/GB18030。
2. 统一字段名、保留来源文件和原始行号。
3. 检查 UID 跨文件重复、关卡覆盖、首局/非首局覆盖、空 replay 数。
4. 空 replay 行写入审计表并跳过主解析。

### 阶段 2：快照与血缘

1. 为配置目录建立只读快照或记录 Git commit。
2. 记录每个配置文件 SHA256。
3. 记录模板 SHA256。
4. 记录分析代码 commit、平台、客户端版本、`iap_cut_a` 假设。

### 阶段 3：模板与初始映射

1. 解析 protobuf 模板。
2. 解析实际/推断配置。
3. 生成候选图案池。
4. 选择旧版或结构化分配。
5. 输出 TileNum 初始映射并缓存。
6. 用 Booster 等数据验证。

### 阶段 4：回放推进

1. 解析所有步骤并累加局内时间。
2. 在每次动作前快照局面。
3. 对点击推进几何和背包。
4. 对自动扩容更新容量，不记玩家选择。
5. 对未实现道具设置不确定性边界。
6. 用 bag/surface 曲线逐步校验。

### 阶段 5：行为特征

1. 计算每步实时特征。
2. 聚合到单次对局。
3. 聚合到“玩家×实例×首局/非首局”。
4. 建立玩家内基线。
5. 聚合跨玩家复刻证据。
6. 输出实例层、配置层、进度层结论。

### 阶段 6：结果校验

将过程标签与以下结果关联：

- 通关率、失败类型；
- 人均道具/消耗；
- 失败流失；
- 重试变化；
- 版本/平台/用户群差异。

目标不是让过程标签复刻消耗标签，而是找到四类有业务价值的组合：

```text
高过程压力 × 高消耗
高过程压力 × 低消耗
低过程压力 × 高消耗
低过程压力 × 低消耗
```

再叠加失败流失，定位“能被道具解决”“挫败后放弃”“便利性消费”“整体顺畅”等不同问题。

---

## 18. 推荐输出文件

至少输出以下表：

1. `input_quality_audit.csv`：来源、行数、空回放、重复 UID、缺字段、版本覆盖。
2. `config_resolution.csv`：每实例命中的 LevelId、DupPara、来源、歧义和哈希。
3. `initial_tile_mapping.csv`：每个 TileNum 的初始坐标、图案、塔和卡点标识。
4. `replay_timeline.csv`：每一步间隔、累计时间、动作、目标和初始图案。
5. `decision_states.csv`：每次决策前后的背包、可见、可点击、路径宽度和选择质量。
6. `attempt_features.csv`：每局行为特征与质量覆盖。
7. `player_instance_features.csv`：每玩家每实例每阶段的聚合及个人基线偏离。
8. `replication_evidence.csv`：效应中位数、方向一致率、置信区间、对半和 LOO 稳定性。
9. `instance_layers.csv`：实例层 L0–L4/P 标签与证据。
10. `level_summary.csv`：进度层综合结论。
11. `method_and_limits.md`：本批次参数、假设、不可还原边界。

每个结果都应允许追溯到 `source_file + source_row_no + user_id + event_time`。

---

## 19. 最小可运行代码入口

当前可复用代码：

```text
/Users/你的用户名/Projects/hhtools/玩家行为分析工具/
├── scripts/analysis/restore_player_replay.py
├── scripts/analysis/analyze_level_behavior_v3.py
├── scripts/analysis/build_full_cohort_conclusion.py
├── scripts/analysis/puzzle_core/template_reader.py
├── scripts/analysis/puzzle_core/protobuf_wire.py
└── scripts/analysis/puzzle_core/geometry.py
```

关键游戏端源码：

```text
/Users/你的用户名/Projects/tile/ulink/TileEnv/TileMatch/Data/LevelGenerator/LevelDupParameter.cs
/Users/你的用户名/Projects/tile/ulink/TileEnv/TileMatch/Data/LevelGenerator/LevelDupGenerator.cs
/Users/你的用户名/Projects/tile/ulink/TileEnv/TileMatch/Data/LevelGenerator/CustomRandom.cs
/Users/你的用户名/Projects/tile/ulink/TileEnv/TileMatch/Data/TileTypeValues.cs
/Users/你的用户名/Projects/tile/ulink/TileEnv/TileMatch/Data/LevelSaveData.cs
/Users/你的用户名/Projects/tile/ulink/TileEnv/TileMatch/Data/TileData.cs
/Users/你的用户名/Projects/tile/ulink/TileEnv/TileMatch/LevelBot/UserActionRecorder.cs
```

直接运行还原器前，应显式传当前模板目录；脚本中的历史默认模板路径可能已过期：

```bash
python3 scripts/analysis/restore_player_replay.py \
  --input /path/to/player.csv \
  --output-dir /path/to/output \
  --config-dir /Users/你的用户名/Projects/ulinkconfig/csv \
  --template-dir /Users/你的用户名/Projects/tile/Tile/Assets/Resources/TileMatchLevelData \
  --iap-cut-a 0
```

具体参数以脚本 `--help` 为准。不要让工具修改模板或配置源目录；运行产物写入独立任务目录。

---

## 20. 接手 AI 的首轮执行清单

另一个 AI 接手后，建议先逐项回答并输出证据：

1. 这批数据的产品、平台、版本、日期和关卡范围是什么？
2. 是否有实际 `level_id`、`level_dup_para_id`、`level_dup_para`？
3. 配置是事件版本快照还是当前配置？Git commit/哈希是什么？
4. 模板目录是否与数据版本匹配？模板 SHA256 是否已记录？
5. 各实例走旧版还是结构化分配？依据是什么？
6. IAP 保护对 A 数量的假设是什么？
7. 有多少局含 C–N、塔、主动道具、自动 708、复活？
8. 初始映射通过多少 Booster 三张一致校验？
9. 几何点击、背包曲线、表面曲线覆盖率分别多少？
10. 哪些指标是精确值，哪些是上下界或点估计？
11. 首局和非首局是否分别聚合？
12. 是否先做玩家内基线，再做跨玩家复刻？
13. 是否避免用四分位数强制均分关卡层级？
14. 样本不足的实例是否标为待补样，而不是硬贴难度标签？

如果这些问题没有答案，不应直接发布“某关难/简单”的结论。

---

## 21. 常见错误

1. 把 `interval_ms` 当累计时间。
2. 把 120000 当精确等待 120 秒。
3. 把 TileNum 当 TileId 或坐标。
4. 解析模板后按坐标重排，破坏 TileNum。
5. 用 Python `random` 替代 `CustomRandom`。
6. 忽略候选池列表顺序。
7. 旧版配额用 Python 双精度代替 Unity float32。
8. 三轮洗牌误写成 Fisher–Yates。
9. 结构化关卡仍走旧版均分洗牌。
10. 用当前配置静默冒充历史实际配置。
11. 只按 `level_index+template_id` 合并不同副本/参数实例。
12. 把 `U1_0` 解释成某个 DDA 难度档。
13. 把自动 708 算作玩家主动道具。
14. 无 533 就断言当局一定是 7 格。
15. 塔或道具发生后仍宣称图案状态完全精确。
16. C–N 图案未知就丢弃整局几何和时间数据。
17. 一行 replay 为空就阻断整个批次。
18. 直接跨玩家比较绝对耗时，不做个人基线。
19. 用失败、消耗或模板静态指标直接决定行为层。
20. 用四分位数强制让各层数量相等。

---

## 22. 当前结论的适用边界

只用模板文件和参数配置表，可以精确或高置信复现“进关时”的 A/B 图案分布，前提是：

- 模板与事件版本一致；
- 实际 `DupPara` 已知，或配置映射唯一；
- 候选图案表及其顺序与客户端一致；
- 选择了正确的旧版/结构化算法；
- 正确复刻 CustomRandom、float32 和随机调用顺序；
- IAP 保护的有效 A 数量已知或明确假设；
- 不把 C–N 的非确定图案伪装成可确定结果。

它不能仅凭静态输入完整复现：

- 玩家进入后塔内运行时换牌；
- 依赖玩家状态的幸运/保护逻辑；
- 洗牌、抓钩、撤回、移动、复活等 Handler 效果；
- 服务端或热更配置与本地快照不一致的部分；
- 未埋点的自动扩容来源。

因此最终报告应同时给出“还原结果”和“证据等级”。真正可靠的分析不是假装所有状态都精确，而是在每个时点明确：哪些是模板确定、哪些是配置确定、哪些由埋点观察、哪些只是推断。

---

## 23. 一句话交接

先用模板序列化顺序建立 `TileNum → 坐标/遮挡`，再用实际 DupPara 的 A/B 种类数和第二段种子，通过客户端同款候选池、`CustomRandom` 及对应的旧版或结构化分配算法恢复初始图案；随后逐步解析 replay，在每次点击前计算玩家真正看见的可见/可点击牌、背包压力和短窗路径宽度，先与该玩家自己的其它关卡比较，再检验其他玩家是否复刻相同偏离，并始终把配置推断、塔/道具动态变化和容量不确定性显式标注出来。
