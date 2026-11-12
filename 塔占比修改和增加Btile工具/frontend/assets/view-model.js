(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TowerReducerViewModel = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const asObject = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const asArray = (value) => Array.isArray(value) ? value : Object.values(asObject(value));
  const asText = (value, fallback = "") => typeof value === "string" && value.trim() ? value : fallback;
  const asNumber = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const asOptionalNumber = (value) => {
    if (value === null || value === undefined || value === "") return null;
    const result = Number(value);
    return Number.isFinite(result) ? result : null;
  };

  function parseTemplateIds(value) {
    return [...new Set(String(value || "").split(/[\s,，;；]+/u).filter(Boolean))];
  }

  function searchHelpTopics(topics, query) {
    const tokens = String(query || "").trim().toLowerCase().split(/\s+/u).filter(Boolean);
    if (!tokens.length) return [];
    return topics.filter((topic) => {
      const text = [topic.title, topic.intro, ...asArray(topic.aliases), String(topic.body || "").replace(/<[^>]*>/g, " ")].join(" ").toLowerCase();
      return tokens.every((token) => text.includes(token));
    });
  }

  function normalizeImported(payload) {
    const rootValue = asObject(payload);
    return {
      importId: asText(rootValue.import_id),
      files: asArray(rootValue.files).map((file, index) => {
        const row = asObject(file);
        const rawInspection = row.inspection || row.template;
        const hasInspection = Boolean(rawInspection && typeof rawInspection === "object" && !Array.isArray(rawInspection));
        const inspection = asObject(rawInspection);
        const issues = asArray(inspection.issues).map((issue) => {
          const item = asObject(issue);
          return {severity: asText(item.severity, "warning"), message: asText(item.message, "模板结构需要人工核对")};
        });
        return {
          key: asText(row.file_id, `file-${index + 1}`),
          fileId: asText(row.file_id),
          sourceSha256: asText(row.sha256),
          name: asText(row.original_name, asText(row.filename, `模板 ${index + 1}`)),
          status: asText(row.status, "unknown"),
          duplicateOf: asText(row.duplicate_of),
          hasInspection,
          templateId: asText(inspection.template_id, asText(row.template_id, "等待解析")),
          towerCount: asOptionalNumber(inspection.tower_count ?? row.tower_count),
          towerStacks: asArray(inspection.stacks ?? inspection.tower_stacks ?? row.tower_stacks)
            .map((stack) => asNumber(asObject(stack).count ?? stack)),
          tileCount: asOptionalNumber(inspection.total_tiles ?? inspection.tile_count ?? row.tile_count),
          aCount: asOptionalNumber(inspection.a_count),
          bCount: asOptionalNumber(inspection.b_count),
          nonTowerACount: asOptionalNumber(inspection.non_tower_a_count),
          nonTowerBCount: asOptionalNumber(inspection.non_tower_b_count),
          towerTileCount: asOptionalNumber(inspection.tower_tile_count),
          ratio: asOptionalNumber(inspection.tower_ratio ?? row.tower_ratio),
          nonTowerLayers: asArray(inspection.non_tower_layers),
          symmetry: asArray(inspection.symmetry),
          profiles: asArray(inspection.profiles),
          issues,
          // Import owns parsing, not retroactive repair of source geometry.
          // Generation reports any conditions that prevent a safe move.
          hasBlockingIssues: row.status === "failed",
          hasWarnings: issues.length > 0,
          error: asObject(row.error),
        };
      }),
    };
  }

  function describeImportInspection(file) {
    const row = asObject(file);
    if (!row.hasInspection) return {available: false, facts: [], issues: []};
    const stacks = asArray(row.towerStacks).map((value) => asNumber(value));
    const symmetryNames = {horizontal: "左右轴对称", vertical: "上下轴对称", rotation: "中心对称"};
    const symmetry = asArray(row.symmetry).map((value) => symmetryNames[value] || String(value));
    const profileNames = [...new Set(asArray(row.profiles).map((profile) => {
      const count = asArray(profile).length;
      if (count === 4) return "一压四";
      if (count === 2) return "一压二";
      return count ? `一压${count}` : "无直接压盖";
    }))];
    const ratio = asOptionalNumber(row.ratio);
    return {
      facts: [
        `A / B Tile：${asNumber(row.aCount)} / ${asNumber(row.bCount)}`,
        `塔：${asNumber(row.towerCount)} 堆，共 ${asNumber(row.towerTileCount)} 张（${stacks.length ? stacks.join(" / ") : "无塔"}）`,
        `塔占比：${ratio === null ? "—" : `${(ratio <= 1 ? ratio * 100 : ratio).toFixed(1)}%`}`,
        `非塔层：${asArray(row.nonTowerLayers).length ? asArray(row.nonTowerLayers).join(" / ") : "—"}`,
        `对称：${symmetry.length ? symmetry.join(" / ") : "未识别稳定对称"}`,
        `堆叠：${profileNames.length ? profileNames.join(" / ") : "未识别非塔堆叠"}`,
      ],
      issues: asArray(row.issues).map((issue) => ({
        severity: asText(asObject(issue).severity, "warning"),
        message: asText(asObject(issue).message, "模板结构需要人工核对"),
      })),
    };
  }

  function normalizeTileCounts(value, allowNegative = false) {
    const counts = asObject(value);
    if (!["A", "B"].every((kind) => Number.isInteger(counts[kind]) && (allowNegative || counts[kind] >= 0))) return null;
    return {A: counts.A, B: counts.B};
  }

  function normalizeTemplate(row, key, index) {
    const item = asObject(row);
    const plan = {...asObject(item.summary), ...asObject(item.plan_summary)};
    const verification = asObject(item.verification);
    const facts = asObject(verification.facts);
    const source = asObject(item.source || item.inspection);
    const parameters = asObject(item.parameters || item.options);
    const naming = asObject(item.naming);
    const artifacts = asArray(item.artifacts);
    const sourceTowerTileCount = asOptionalNumber(plan.original_tower_tiles ?? plan.source_tower_tile_count ?? source.tower_tile_count);
    const operation = asText(plan.operation, ["increase", "ordinary_add"].includes(parameters.mode) ? parameters.mode : "reduce");
    const rawError = asObject(item.error);
    const legacyIncreaseLimit = operation === "increase" && ["search_budget", "increase_search_budget"].includes(rawError.code);
    const error = legacyIncreaseLimit ? {...rawError,
      message: "自动摆放达到本次尝试时限或次数上限，暂未找到完整方案。",
      recommendations: ["本次试算未完成，尚未判断有无可行方案；可单独调整增加方式、数量或区域后重新预览。"],
    } : rawError;
    return {
      operation,
      manualEdit: asObject(plan.manual_edit),
      sourceGeometryWarnings: asArray(item.source_geometry_warnings),
      outputGeometryWarnings: asArray(item.output_geometry_warnings),
      adjusted: Boolean(plan.adjusted || Object.keys(asObject(plan.manual_edit)).length || asObject(plan.translation).dx || asObject(plan.translation).dy),
      increaseSummary: ["increase", "ordinary_add"].includes(operation) ? plan : null,
      key: asText(item.key, key || `template-${index + 1}`),
      name: asText(asArray(item.original_names)[0], asText(item.original_name, asText(item.name, `模板 ${index + 1}`))),
      templateId: asText(item.source_template_id, asText(item.template_id, asText(source.template_id, "—"))),
      outputId: asText(plan.new_id, asText(naming.new_id, asText(item.output_template_id, asText(artifacts[0]?.new_id, "—")))),
      status: asText(item.status, "unknown"),
      stage: asText(item.stage),
      attemptId: asNumber(item.current_attempt_id ?? item.attempt_id),
      progress: asObject(item.progress),
      oldRatio: asOptionalNumber(plan.source_tower_ratio ?? item.original_ratio ?? source.tower_ratio),
      newRatio: asOptionalNumber(plan.final_ratio ?? plan.result_tower_ratio ?? facts.tower_ratio ?? item.new_ratio),
      retained: asArray(plan.retained_counts ?? item.retained_counts),
      sourceTowerCounts: asArray(plan.source_tower_counts),
      sourceTowerTileCount,
      resultTowerTileCount: asOptionalNumber(plan.final_tower_tiles ?? plan.result_tower_tile_count),
      isNoTower: operation === "reduce" && sourceTowerTileCount === 0,
      placement: asArray(plan.destinations).length
        ? `${asArray(plan.destinations).length} 张 · 新增 ${asNumber(plan.added_layers)} 层`
        : asText(item.placement_summary, "—"),
      moveType: asText(plan.move_type, "—"),
      moveCount: asOptionalNumber(plan.move_count),
      sourceTileCounts: normalizeTileCounts(plan.source_tile_counts),
      aCount: asOptionalNumber(source.a_count ?? asObject(plan.source_tile_counts).A),
      bCount: asOptionalNumber(source.b_count ?? asObject(plan.source_tile_counts).B),
      nonTowerACount: asOptionalNumber(item.non_tower_a_count ?? source.non_tower_a_count ?? plan.non_tower_a_count),
      nonTowerBCount: asOptionalNumber(item.non_tower_b_count ?? source.non_tower_b_count ?? plan.non_tower_b_count),
      removedTileCounts: normalizeTileCounts(plan.removed_tile_counts),
      addedTileCounts: normalizeTileCounts(plan.added_tile_counts),
      resultTileCounts: normalizeTileCounts(plan.result_tile_counts),
      tileCountChanges: normalizeTileCounts(plan.tile_count_changes, true),
      addedLayers: asNumber(plan.added_layers),
      strategy: asText(plan.strategy, asText(parameters.strategy)),
      seed: asNumber(plan.seed, asNumber(parameters.seed)),
      sourceSha256: asText(plan.source_sha256, asText(item.source_sha256)),
      outputSha256: asText(plan.output_sha256, asText(asObject(artifacts[0]).sha256)),
      destinations: asArray(plan.destinations),
      audit: asObject(plan.audit),
      recommendations: asArray(legacyIncreaseLimit ? error.recommendations : plan.recommendations ?? error.recommendations).map(String),
      parameterAdvice: asObject(plan.parameter_advice),
      recommendation: legacyIncreaseLimit ? error.message : asText(item.recommendation, asText(error.message)),
      error,
      verification: {
        status: asText(verification.status, "unavailable"),
        ok: Boolean(verification.ok),
        facts,
        issues: asArray(verification.issues),
      },
      plan,
      parameters,
      naming,
      artifacts,
      attempts: asArray(item.attempts),
    };
  }

  function tileMovementView(row) {
    if (["increase", "ordinary_add"].includes(row.operation)) {
      const s = asObject(row.increaseSummary), added = asObject(s.added_tiles);
      const original = asArray(s.original_counts), final = asArray(s.final_counts);
      const amount = original.length === 2 && final.length === 2 ? `A ${final[0] - original[0]} / B ${final[1] - original[1]}`
        : typeof s.added_tiles === "number" ? `${s.added_tiles} 张` : `A ${added.A ?? "—"} / B ${added.B ?? "—"}`;
      return {summary: `新增 ${amount}`, changeSummary: `总量 ${s.original_total ?? "—"} → ${s.final_total ?? "—"} 张`,
        countSummary: `塔内 ${s.original_tower_tiles ?? "—"} → ${s.final_tower_tiles ?? "—"} 张`, hasCountEvidence: true};
    }
    const legacySummary = row.moveCount === null ? "—" : row.moveCount === 0 ? "无需搬移" : `${row.moveType} ${row.moveCount} 张`;
    const counts = [row.sourceTileCounts, row.removedTileCounts, row.addedTileCounts, row.resultTileCounts, row.tileCountChanges];
    if (counts.some((value) => !value)) return {summary: legacySummary, changeSummary: "", countSummary: "", hasCountEvidence: false};
    const describe = (value) => ["A", "B"].filter((kind) => value[kind] > 0).map((kind) => `${kind} ${value[kind]} 张`).join("、");
    const sameTypes = ["A", "B"].every((kind) => row.removedTileCounts[kind] === row.addedTileCounts[kind]);
    const changes = ["A", "B"].filter((kind) => row.tileCountChanges[kind] !== 0)
      .map((kind) => `${kind} ${row.tileCountChanges[kind] < 0 ? "减少" : "增加"} ${Math.abs(row.tileCountChanges[kind])} 张`);
    return {
      summary: row.plan?.manual_recovery ? `清塔移除 ${describe(row.removedTileCounts) || "0 张"} → 手动补入 ${describe(row.addedTileCounts) || "0 张"}` : row.moveCount === 0 ? "无需搬移" : sameTypes ? `搬移 ${describe(row.removedTileCounts)}`
        : `移除 ${describe(row.removedTileCounts)} → 补入 ${describe(row.addedTileCounts)}`,
      changeSummary: changes.join("，"),
      countSummary: changes.length
        ? `A/B 总量：${row.sourceTileCounts.A} / ${row.sourceTileCounts.B} → ${row.resultTileCounts.A} / ${row.resultTileCounts.B}`
        : `A/B 总量不变：${row.resultTileCounts.A} / ${row.resultTileCounts.B}`,
      hasCountEvidence: true,
    };
  }

  function normalizeJob(payload, fallbackJobId = "") {
    const rootValue = asObject(payload);
    const rawTemplates = rootValue.templates;
    const templates = Array.isArray(rawTemplates)
      ? rawTemplates.map((row, index) => normalizeTemplate(row, "", index))
      : Object.entries(asObject(rawTemplates)).map(([key, row], index) => normalizeTemplate(row, key, index));
    const progress = asObject(rootValue.progress);
    return {
      id: asText(rootValue.job_id, fallbackJobId),
      status: asText(rootValue.status, "unknown"),
      revision: asNumber(rootValue.revision),
      createdAt: asText(rootValue.created_at),
      updatedAt: asText(rootValue.updated_at),
      progress: {
        completed: asNumber(progress.completed_templates ?? progress.completed),
        total: asNumber(progress.total_templates ?? progress.total, templates.length),
        percent: asNumber(progress.percent),
        stage: asText(progress.stage, asText(rootValue.stage)),
      },
      templates,
      artifacts: asArray(rootValue.artifacts),
      logs: asArray(rootValue.logs),
    };
  }

  function parameterAdviceView(row) {
    if (row.operation === "ordinary_add" || asObject(row.parameters).mode === "ordinary_add") return ordinaryParameterAdviceView(row);
    if (row.operation === "increase" || asObject(row.parameters).mode === "increase") return increaseParameterAdviceView(row);
    const advice = asObject(row.parameterAdvice);
    const current = asObject(row.parameters);
    const names = {min_ratio: "塔 Tile 占比下限", ratio: "塔 Tile 占比上限", replenish_type: "非塔补牌类型", strategy: "补牌位置策略", max_tiles_per_layer: "单层非塔 Tile 上限", max_added_layers: "最多新增层数", seed: "随机种子", retained_counts: "逐塔保留数"};
    const strategies = {top_first: "顶层优先", bottom_first: "底层优先", balanced: "五五开", seeded_random: "随机"};
    const format = (field, value) => ["min_ratio", "ratio"].includes(field) ? `${Number((Number(value) * 100).toFixed(3))}%`
      : field === "replenish_type" ? ({auto:"自动",A:"全部补 A",B:"全部补 B",majority_A:"以 A 为主",majority_B:"以 B 为主",preserve:"与原搬出牌类型一致"})[value] || "未识别类型"
      : field === "strategy" ? strategies[value] || "未识别策略"
      : field === "retained_counts" ? value === null ? "自动分配" : asArray(value).join(" / ")
      : `${value}${field === "max_tiles_per_layer" ? "张" : field === "max_added_layers" ? "层" : ""}`;
    const schemes = asArray(advice.schemes).filter((scheme) => {
      const p = asObject(scheme.parameters);
      return scheme.verified === true && Number.isFinite(p.ratio) && p.ratio >= 0 && p.ratio <= asNumber(current.ratio, .25)
        && validateRatioRangePercent((p.min_ratio ?? current.min_ratio ?? 0) * 100, p.ratio * 100).ok
        && ["auto", "A", "B", "majority_A", "majority_B", "preserve"].includes(p.replenish_type ?? current.replenish_type ?? "auto")
        && Boolean(strategies[p.strategy]) && Number.isInteger(p.max_tiles_per_layer) && p.max_tiles_per_layer > 0
        && Number.isInteger(p.max_added_layers) && p.max_added_layers >= 0 && Number.isInteger(p.seed)
        && asArray(scheme.changes).length && asArray(scheme.changes).every((change) => names[change.field]
          && JSON.stringify(change.from) === JSON.stringify(current[change.field])
          && JSON.stringify(change.to) === JSON.stringify(p[change.field]));
    }).slice(0, 2).map((scheme) => {
      const changed = new Set(scheme.changes.map((change) => change.field));
      const p = scheme.parameters;
      const facts = asObject(scheme.facts);
      return {title: p.strategy === current.strategy ? `保留${strategies[p.strategy]}` : `改用${strategies[p.strategy]}`,
        parameters: p,
        changedFields: [...changed],
        adjustments: scheme.changes.map((change) => `${names[change.field]}：${format(change.field, change.from)} → ${format(change.field, change.to)}`),
        unchanged: ["min_ratio", "ratio", "replenish_type", "strategy", "max_tiles_per_layer", "max_added_layers"].filter((field) => p[field] !== undefined && !changed.has(field))
          .map((field) => `${names[field]} ${format(field, p[field])}`).join("；"),
        outcome: `试算通过独立验收：实际塔占比 ${Number((asNumber(facts.tower_ratio) * 100).toFixed(1))}%，搬移 ${asNumber(facts.move_count)} 张，实际新增 ${asNumber(facts.added_layers)} 层`,
      };
    });
    const recovery = asArray(asObject(row.error).recommendations).map((value) => asText(value)).filter(Boolean).join("；");
    const message = schemes.length ? `找到 ${schemes.length} 个已验收方案；选择后填入参数，再重新生成。`
      : recovery || (asObject(row.error).code === "single_move_type_unavailable"
        ? "点击“单独调整并重新运行”，保留原参数重算；当前规则允许移出与补入不同类型的 Tile。" : "")
      || (!Object.keys(advice).length ? "旧任务尚无已验收参数方案，请重新运行该模板获取建议。"
      : advice.status === "limited" ? "本次试算范围内未找到已验收方案，可单独调整后重跑；不能据此认定模板无解。"
      : "未找到已验收参数方案，不会推荐未经验证的数值；请单独调整后重跑或人工检查模板。");
    return {schemes, message};
  }

  function ordinaryParameterAdviceView(row) {
    const advice = asObject(row.parameterAdvice), current = asObject(asObject(row.parameters).ordinary_add);
    const names = {tile_type:"新增类型",added_tiles:"新增张数",placement_strategy:"补入位置",layer_mode:"新增层数方式",min_added_layers:"最少新增层数",max_added_layers:"最多新增层数",target_mode:"数量目标",min_total_tiles:"目标总数下限",max_total_tiles:"目标总数上限",split_mode:"上下分配",top_tiles:"上方张数",bottom_tiles:"下方张数"};
    const labels = {bottom_first:"非塔区域最下层",top_first:"非塔区域最上层",both:"上下都补",auto:"自动",custom:"自定义",range:"范围",exact:"固定值",count:"新增张数",total_range:"目标总数范围"};
    const same = (a,b) => JSON.stringify(stable(a)) === JSON.stringify(stable(b));
    const valid = p => Object.keys(names).every(k => Object.hasOwn(p,k)) && Object.keys(p).every(k=>Object.hasOwn(names,k))
      && ['A','B'].includes(p.tile_type) && ['bottom_first','top_first','both'].includes(p.placement_strategy)
      && ['auto','range','exact'].includes(p.layer_mode) && ['auto','custom'].includes(p.split_mode)
      && ['count','total_range'].includes(p.target_mode)
      && ['added_tiles','min_added_layers','max_added_layers','min_total_tiles','max_total_tiles','top_tiles','bottom_tiles'].every(k=>Number.isInteger(p[k])&&p[k]>=0&&p[k]<2**31)
      && p.added_tiles>0 && p.added_tiles%3===0 && p.min_added_layers<=p.max_added_layers
      && p.min_total_tiles>0 && p.min_total_tiles<=p.max_total_tiles
      && (p.layer_mode!=='exact'||p.min_added_layers===p.max_added_layers)
      && (p.placement_strategy!=='both'||p.split_mode!=='custom'||p.top_tiles>0&&p.bottom_tiles>0&&(p.top_tiles+p.bottom_tiles)%3===0&&(p.target_mode!=='count'||p.top_tiles+p.bottom_tiles===p.added_tiles));
    const schemes = asArray(advice.schemes).filter(s=> {
      if (!s || typeof s !== 'object' || !Array.isArray(s.changes) || s.changes.some(c=>!c || typeof c !== 'object')) return false;
      const p=asObject(asObject(s.parameters).ordinary_add), f=asObject(s.facts), changes=asArray(s.changes);
      return advice.operation==='ordinary_add' && same(advice.current_options,current) && valid(p)
        && s.verified===true && asObject(s.parameters).mode==='ordinary_add' && p.tile_type===current.tile_type
        && /^[a-f0-9]{64}$/.test(s.output_sha256||'') && /^[a-f0-9]{64}$/.test(row.sourceSha256||'')
        && s.source_sha256===row.sourceSha256 && advice.source_sha256===row.sourceSha256
        && changes.length>0 && same(changes.map(c=>c.field).sort(),Object.keys(names).filter(k=>!same(p[k],current[k])).sort())
        && changes.every(c=>Object.hasOwn(names,c.field)&&same(c.from,current[c.field])&&same(c.to,p[c.field]))
        && ['original_total','final_total','added_tiles','top_added_tiles','bottom_added_tiles','added_layers'].every(k=>Number.isInteger(f[k])&&f[k]>=0)
        && f.added_tiles>0 && f.added_tiles%3===0 && f.final_total===f.original_total+f.added_tiles
        && f.added_tiles===f.top_added_tiles+f.bottom_added_tiles
        && (p.target_mode==='count'?f.added_tiles===p.added_tiles:f.final_total>=p.min_total_tiles&&f.final_total<=p.max_total_tiles);
    }).slice(0,2).map(s=>({title:s.parameters.ordinary_add.placement_strategy===current.placement_strategy?'保留补入位置':'保留数量，调整补入位置',parameters:s.parameters,
      changedFields:s.changes.map(c=>c.field),adjustments:s.changes.map(c=>`${names[c.field]}：${labels[c.from]||c.from} → ${labels[c.to]||c.to}`),
      unchanged:`新增 ${current.tile_type}；原阵型、对称、边界及底层支撑规则`,
      outcome:`已通过独立验收：总量 ${s.facts.original_total} → ${s.facts.final_total} 张；上方 ${s.facts.top_added_tiles} 张，下方 ${s.facts.bottom_added_tiles} 张，新增 ${s.facts.added_layers} 层`}));
    return {schemes,message:schemes.length?'以下参数已试算并通过独立验收，应用后仅重新生成当前模板。':Object.keys(advice).length
      ?'已尝试调整层数、补入位置及数量，暂未获得通过验收的推荐方案；不会把未经验证的数值推荐给你。这不代表已证明所有摆法都不可行。'
      :row.status==='no_op' ? asArray(row.recommendations).join('；') : '旧任务尚无已验收推荐方案，请保留原参数单独重跑以获取建议。'};
  }

  function increaseParameterAdviceView(row) {
    const advice = asObject(row.parameterAdvice), current = asObject(asObject(row.parameters).increase);
    const names = {target_mode: "数量目标", added_tiles: "期望新增张数", total_multiplier: "最终总量 / 原总量", min_ratio: "塔占比下限", max_ratio: "塔占比上限",
      operation: "增加方式", stack_count: "新增堆数", regions: "摆放区域", fill_type: "新增 Tile 类型", direction: "新塔默认方向", spread: "摆放密度",
      spacing: "塔与塔间距", formation_gap: "塔与主阵型间距", translate_x: "整体平移 X", translate_y: "整体平移 Y", auto_translate: "空间不足时推荐整体平移",
      height_mode: "堆高关系", tower_counts: "逐塔最终张数", tower_positions: "逐塔位置", tower_directions: "逐塔方向", locked_towers: "锁定塔堆"};
    const labels = {target_mode: {ratio: "最终塔占比", count: "新增张数", multiplier: "最终总量倍数", manual: "逐塔最终张数"},
      operation: {new: "新建塔", existing: "原塔加层", both: "原塔加层 + 新建塔"},
      fill_type: {auto: "自动", A: "新增 A", B: "新增 B"}, spread: {compact: "紧凑", distributed: "分散"},
      height_mode: {difference: "保留高低差值", proportion: "保留高度比例", manual: "手动高度"}};
    const directions = ["朝上", "朝左", "朝下", "朝右"], regions = {auto: "自动", top: "上", bottom: "下", left: "左", right: "右", center: "内部空白"};
    const same = (left, right) => JSON.stringify(stable(left)) === JSON.stringify(stable(right));
    const integer = value => Number.isInteger(value) && value >= 0;
    const optionsValid = p => Object.keys(names).every(field => Object.hasOwn(p, field)) && Object.keys(p).every(field => Object.hasOwn(names, field))
      && Object.entries(labels).every(([field, choices]) => Object.hasOwn(choices, p[field]))
      && ["added_tiles", "total_multiplier", "min_ratio", "max_ratio"].every(field => Number.isFinite(p[field]))
      && (p.target_mode !== "count" || p.added_tiles > 0) && (p.target_mode !== "multiplier" || p.total_multiplier > 1)
      && (p.target_mode !== "ratio" || p.min_ratio >= 0 && p.min_ratio < p.max_ratio && p.max_ratio <= 1)
      && (p.stack_count === null || integer(p.stack_count)) && Array.isArray(p.regions) && p.regions.length > 0 && p.regions.every(value => Object.hasOwn(regions, value))
      && integer(p.direction) && p.direction < 4 && ["spacing", "formation_gap"].every(field => integer(p[field]))
      && ["translate_x", "translate_y"].every(field => Number.isInteger(p[field])) && typeof p.auto_translate === "boolean"
      && Array.isArray(p.tower_counts) && p.tower_counts.every(value => integer(value) && value > 0) && (p.target_mode !== "manual" || p.tower_counts.length > 0)
      && Array.isArray(p.tower_positions) && p.tower_positions.every(value => value && integer(value.index) && Number.isInteger(value.x) && Number.isInteger(value.y))
      && Array.isArray(p.tower_directions) && p.tower_directions.every(value => value && integer(value.index) && integer(value.direction) && value.direction < 4)
      && Array.isArray(p.locked_towers) && p.locked_towers.every(integer);
    const format = (field, value) => labels[field] ? labels[field][value]
      : ["min_ratio", "max_ratio"].includes(field) ? `${Number((value * 100).toFixed(3))}%`
      : field === "direction" ? directions[value]
      : field === "auto_translate" ? value ? "开启" : "关闭"
      : field === "stack_count" ? value === null || value === 0 ? "自动" : `${value} 堆`
      : field === "regions" ? value.map(region => regions[region]).join(" / ")
      : field === "tower_counts" ? value.length ? `${value.join(" / ")} 张` : "自动"
      : field === "tower_positions" ? value.length ? value.map(tower => `第 ${tower.index + 1} 堆 (${tower.x}, ${tower.y})`).join("；") : "自动"
      : field === "tower_directions" ? value.length ? value.map(tower => `第 ${tower.index + 1} 堆 ${directions[tower.direction]}`).join("；") : "保留默认"
      : field === "locked_towers" ? value.length ? value.map(index => `第 ${index + 1} 堆`).join(" / ") : "无锁定"
      : `${value}${field === "added_tiles" ? " 张" : ["spacing", "formation_gap", "translate_x", "translate_y"].includes(field) ? " 格" : ""}`;
    const schemes = advice.operation === "increase" ? asArray(advice.schemes).filter(scheme => {
      if (!scheme || typeof scheme !== "object" || !Array.isArray(scheme.changes) || scheme.changes.some(change => !change || typeof change !== "object")) return false;
      const parameters = asObject(scheme.parameters), p = asObject(parameters.increase), facts = asObject(scheme.facts), changes = asArray(scheme.changes);
      if (scheme.verified !== true || parameters.mode !== "increase" || !optionsValid(p) || !optionsValid(current) || !same(p, scheme.options)) return false;
      if (!/^[a-f0-9]{64}$/.test(scheme.source_sha256) || !/^[a-f0-9]{64}$/.test(scheme.output_sha256)
        || scheme.source_sha256 !== advice.source_sha256 || row.sourceSha256 && scheme.source_sha256 !== row.sourceSha256) return false;
      const changedFields = Object.keys(names).filter(field => !same(current[field], p[field]));
      if (!changes.length || changes.length !== changedFields.length || new Set(changes.map(change => change.field)).size !== changes.length
        || !changes.every(change => changedFields.includes(change.field) && same(change.from, current[change.field]) && same(change.to, p[change.field]))) return false;
      return ["added_tiles", "final_total", "final_tower_tiles", "new_tower_count", "extended_tower_count"].every(field => integer(facts[field]))
        && facts.final_total > 0 && facts.final_tower_tiles <= facts.final_total && facts.added_tiles <= facts.final_total
        && Number.isFinite(facts.tower_ratio) && Math.abs(facts.tower_ratio - facts.final_tower_tiles / facts.final_total) < .000001
        && Array.isArray(facts.tower_counts) && facts.tower_counts.every(value => integer(value) && value > 0)
        && facts.tower_counts.reduce((sum, value) => sum + value, 0) === facts.final_tower_tiles
        && facts.new_tower_count + facts.extended_tower_count <= facts.tower_counts.length
        && Array.isArray(facts.final_counts) && facts.final_counts.length === 2 && facts.final_counts.every(integer)
        && facts.final_counts[0] + facts.final_counts[1] === facts.final_total;
    }).slice(0, 2).map(scheme => {
      const p = scheme.parameters.increase, facts = scheme.facts, changed = new Set(scheme.changes.map(change => change.field));
      const targetFields = p.target_mode === "count" ? ["added_tiles"] : p.target_mode === "multiplier" ? ["total_multiplier"] : p.target_mode === "ratio" ? ["min_ratio", "max_ratio"] : ["tower_counts"];
      return {title: asText(scheme.title, "调整增加参数"), parameters: scheme.parameters, changedFields: [...changed],
        adjustments: scheme.changes.map(change => `${names[change.field]}：${format(change.field, change.from)} → ${format(change.field, change.to)}`),
        unchanged: [...targetFields, "operation", "fill_type", "regions"].filter(field => !changed.has(field)).map(field => `${names[field]} ${format(field, p[field])}`).join("；"),
        outcome: `试算通过独立验收：新增 ${facts.added_tiles} 张；新建 ${facts.new_tower_count} 堆，原塔加层 ${facts.extended_tower_count} 堆；最终总量 ${facts.final_total} 张，塔占比 ${Number((facts.tower_ratio * 100).toFixed(2))}%`,
      };
    }) : [];
    const recovery = asArray(asObject(row.error).recommendations).map(value => asText(value)).filter(Boolean).join("；");
    const errorCode = asObject(row.error).code;
    const towerlessMessage = ["new_tower_positions_unavailable", "increase_new_tower_positions_unavailable"].includes(errorCode)
      ? "原模板没有可加层的塔；当前约束下也没有合规的新塔位置，无法完成本次增加。"
      : ["search_budget", "increase_search_budget"].includes(errorCode)
      ? "原模板没有可加层的塔；本次试算未完成，尚未判断能否新建塔，请调整参数后重新预览。"
      : "原模板没有可加层的塔；请根据上方原因调整新增塔参数后重新预览。";
    const message = schemes.length ? `找到 ${schemes.length} 个已验收方案；一键应用只填入当前模板，之后可预览或重新生成。`
      : advice.status === "limited" ? "本次试算范围内未找到已验收方案，可单独调整后重跑；不能据此认定模板无解。"
      : advice.status === "not_applicable" ? towerlessMessage
      : recovery || (!Object.keys(advice).length ? "旧任务尚无已验收的增量参数方案，请单独重新运行该模板获取建议。"
      : "未找到已验收参数方案，请单独调整后重跑；不会推荐未经验证的数值。");
    return {schemes, message};
  }

  function validateRatioRangePercent(minimum, maximum, sourceMaximum = null, templateId = "") {
    if (!Number.isFinite(maximum) || maximum < 0 || maximum > 100) {
      return {ok: false, field: "ratio", message: "塔占比上限只可设为 0–100%。"};
    }
    if (!Number.isFinite(minimum) || minimum < 0 || minimum > 100) {
      return {ok: false, field: "min_ratio", message: "塔占比下限只可设为 0–100%。"};
    }
    if (minimum > maximum || (maximum > 0 && minimum === maximum)) {
      return {ok: false, field: "min_ratio", message: "下限需小于上限；清空塔时上下限都填 0。"};
    }
    if (Number.isFinite(sourceMaximum) && sourceMaximum > 0 && maximum > sourceMaximum) {
      return {ok: false, field: "ratio", message: `模板 ${templateId || "当前模板"} 的塔占比上限不能高于原模板塔占比（${Number(sourceMaximum.toFixed(4))}%）。`};
    }
    return {ok: true, field: "", message: ""};
  }

  function replenishRecommendation(row) {
    const item = asObject(row);
    const a = asOptionalNumber(item.nonTowerACount);
    const b = asOptionalNumber(item.nonTowerBCount);
    if (a === null || b === null || !Number.isInteger(a) || !Number.isInteger(b) || a < 0 || b < 0) {
      return {type: null, message: "原模板非塔 A/B 数量未记录，暂无推荐。"};
    }
    if (a === 0 && b === 0) return {type: null, message: "原模板没有非塔 A/B Tile，暂无推荐。"};
    if (a === b) return {type: null, message: `非塔 A ${a} / B ${b}，补 A 或 B 均可。`};
    const type = a > b ? "A" : "B";
    return {type, message: `推荐补 ${type}（非塔 A ${a} / B ${b}）`};
  }

  function replenishTypeWarning(row, type) {
    if (!["A", "B"].includes(type)) return "";
    const item = asObject(row);
    const count = asOptionalNumber(type === "A" ? item.aCount : item.bCount);
    return count === 0 ? `原模板没有 ${type}；新增类型需手动设置副本基准，当前参数计算不能从 0 按比例推算。` : "";
  }

  function summarizeCommitItems(items) {
    const summary = {written: 0, skipped: 0, failed: 0, rolledBack: 0, backups: 0};
    const backupPaths = new Set();
    asArray(items).forEach((raw) => {
      const row = asObject(raw);
      const status = asText(row.status, asText(row.result));
      if (["exported", "written", "committed", "synced", "created", "replaced"].includes(status)) summary.written += 1;
      else if (status.includes("rollback") || status.includes("rolled_back")) summary.rolledBack += 1;
      else if (["skipped", "skip", "same_sha", "skipped_same_sha", "skipped_conflict", "identical"].includes(status)) summary.skipped += 1;
      else if (["failed", "blocked", "error", "recovery_required"].includes(status)) summary.failed += 1;
      [row, ...asArray(row.targets).map(asObject)].forEach((target) => {
        const backup = asObject(target.backup);
        const path = asText(target.backup_path, asText(backup.relative_path, asText(target.backup)));
        if (path) backupPaths.add(path);
      });
    });
    summary.backups = backupPaths.size;
    return summary;
  }

  function interpretSyncReport(payload) {
    const report = asObject(payload);
    const status = asText(report.status, "unknown");
    const rows = asArray(report.rows);
    const summary = summarizeCommitItems(rows);
    const rowStatuses = rows.map((row) => asText(asObject(row).status));
    const confirmationRequired = status === "confirmation_required";
    const auditFailed = status === "audit_failed";
    const recoveryRequired = ["rollback_failed", "recovery_required", "audit_failed"].includes(status)
      || rowStatuses.some((rowStatus) => ["rollback_failed", "recovery_required"].includes(rowStatus));
    const requiredConflictIds = [...new Set(rows.flatMap((row) => {
      const error = asObject(asObject(row).error);
      const evidence = asObject(error.evidence || error.details);
      return asArray(evidence.required_conflict_ids).map(String);
    }))];
    const allWriteAttemptsRolledBack = summary.rolledBack > 0
      && summary.written === 0
      && rowStatuses.every((rowStatus) => [
        "rolled_back", "skipped", "skip", "same_sha", "skipped_same_sha",
        "skipped_conflict", "identical", "failed", "blocked", "error",
      ].includes(rowStatus));
    return {
      status,
      rows,
      summary,
      confirmationRequired,
      requiredConflictIds,
      auditFailed,
      recoveryRequired,
      allWriteAttemptsRolledBack,
      hasMixedCommittedAndRolledBack: summary.written > 0 && summary.rolledBack > 0,
      consumePlan: !confirmationRequired,
      closeDialog: !confirmationRequired && !recoveryRequired && !allWriteAttemptsRolledBack,
    };
  }

  function summarizeTemplateOutcomes(templates) {
    const summary = {passed: 0, failed: 0, noOp: 0};
    asArray(templates).forEach((raw) => {
      const row = asObject(raw);
      const status = asText(row.status);
      if (["failed", "interrupted", "cancelled"].includes(status)) {
        summary.failed += 1;
        return;
      }
      if (!["verified", "succeeded", "exported", "no_op", "no_tower"].includes(status)) return;
      const savedRecovery=asObject(row.plan).manual_recovery===true&&asArray(row.artifacts).length>0;
      if (Boolean(row.isNoTower) || ["no_op", "no_tower"].includes(status) || (!savedRecovery&&!["increase", "ordinary_add"].includes(row.operation) && row.moveCount === 0)) {
        summary.noOp += 1;
      } else {
        summary.passed += 1;
      }
    });
    return summary;
  }

  function canRerunTemplate(row) {
    return row.stage !== "import" && ["verified", "succeeded", "exported", "failed", "interrupted", "cancelled", "no_op", "no_tower"].includes(row.status);
  }

  function namingRegenerationParameters(parameters, namingRule) {
    const options = {...parameters, naming_rule:namingRule};
    delete options.new_id;
    return options;
  }

  function validateRetainedCounts(values, sourceCounts) {
    const retained = asArray(values);
    const source = asArray(sourceCounts).map((value) => asNumber(value));
    if (retained.length !== source.length) {
      return {ok: false, message: `需要填写 ${source.length} 个保留数，当前为 ${retained.length} 个。`};
    }
    for (let index = 0; index < source.length; index += 1) {
      const value = retained[index];
      if (!Number.isInteger(value) || value < 0 || value > source[index]) {
        return {ok: false, message: `第 ${index + 1} 堆保留数只可为 0–${source[index]} 的整数。`};
      }
    }
    return {ok: true, message: ""};
  }

  function shouldAcceptJobResponse(context) {
    const row = asObject(context);
    if (row.requestGeneration !== row.activeGeneration) return false;
    if (!row.requestJobId || row.requestJobId !== row.currentJobId) return false;
    if (row.currentJobIdForRevision === row.requestJobId) {
      const currentRevision = asOptionalNumber(row.currentRevision);
      const responseRevision = asOptionalNumber(row.responseRevision);
      if (currentRevision !== null && (responseRevision === null || responseRevision < currentRevision)) return false;
    }
    return true;
  }

  function normalizeSyncPlanEntries(rows) {
    return asArray(rows).flatMap((raw, rowIndex) => {
      const row = asObject(raw);
      const parentStatus = asText(row.status, "planned");
      const targets = asArray(row.targets);
      const base = {
        filename: asText(row.filename, `文件 ${rowIndex + 1}`),
        parentStatus,
        parentError: asObject(row.error),
      };
      if (!targets.length) {
        const targetClassification = asText(row.classification, parentStatus === "blocked" ? "blocked" : "ready");
        return [{...row, ...base, targetClassification, classification: parentStatus === "blocked" ? "blocked" : targetClassification}];
      }
      return targets.map((rawTarget) => {
        const target = asObject(rawTarget);
        const targetClassification = asText(target.classification, asText(target.status, "ready"));
        return {
          ...target,
          ...base,
          targetClassification,
          classification: parentStatus === "blocked" ? "blocked" : targetClassification,
          error: target.error || row.error || null,
        };
      });
    });
  }

  function normalizeHistoryRows(recentJobs, currentJob = null) {
    let rows = [...asArray(recentJobs)];
    const current = asObject(currentJob);
    const currentId = asText(current.job_id, asText(current.id));
    if (currentId) {
      const previous = rows.find((row) => asText(asObject(row).job_id, asText(asObject(row).id)) === currentId);
      rows = rows.filter((row) => asText(asObject(row).job_id, asText(asObject(row).id)) !== currentId);
      const merged = {...asObject(previous), ...current};
      const currentUpdated = asText(current.updated_at, asText(current.updatedAt));
      const previousUpdated = asText(asObject(previous).updated_at, asText(asObject(previous).updatedAt));
      merged.updated_at = currentUpdated || previousUpdated;
      rows.unshift(merged);
    }
    return rows.map((raw) => {
      const row = asObject(raw);
      return {
        id: asText(row.job_id, asText(row.id)),
        updated: asText(row.updated_at, asText(row.updatedAt, "—")),
        count: asNumber(asObject(row.progress).total_templates ?? asObject(row.progress).total ?? row.template_count ?? row.input_count ?? asArray(row.templates).length),
        status: asText(row.status, "unknown"),
      };
    }).filter((row) => row.id);
  }

  function resolveCandidate(candidates, current) {
    const rows = asArray(candidates).map(String);
    // A previously confirmed custom path remains authoritative even when a
    // bounded discovery pass does not rediscover it.
    if (current) return String(current);
    return rows.length === 1 ? rows[0] : "";
  }

  function nextPollDelay(failures) {
    return Math.min(8000, 900 * (2 ** Math.min(Math.max(0, Number(failures) || 0), 4)));
  }

  function stable(value) {
    if (Array.isArray(value)) return value.map(stable);
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]));
    }
    return value;
  }

  function planFingerprint(context) {
    return JSON.stringify(stable(asObject(context)));
  }

  function validateRatioPercent(value) {
    const ratio = Number(value);
    if (!Number.isFinite(ratio) || ratio < 0 || ratio > 100) {
      throw new RangeError("塔 Tile 占比只可设为 0–100%。");
    }
    return ratio;
  }

  function navigationScrollBehavior(prefersReducedMotion) {
    return prefersReducedMotion ? "auto" : "smooth";
  }

  function resolveFlowStep(requestedStep, importId) {
    const step = String(requestedStep);
    if (step === "2" && !asText(importId)) {
      return {
        step: "1",
        blocked: true,
        message: "历史任务的原始文件需重新选择并检查。",
      };
    }
    return {step, blocked: false, message: ""};
  }

  function validateNamingRule(value, originalIds = []) {
    const token = "{原模板ID}";
    const rule = typeof value === "string" ? value.trim() : "";
    if (!rule) return {ok: false, message: "命名规则不能为空。", outputIds: []};
    const tokenCount = rule.split(token).length - 1;
    const literal = rule.split(token).join("");
    if (tokenCount > 1 || literal.includes("{") || literal.includes("}")) {
      return {ok: false, message: "命名中最多使用一个 {原模板ID}；也可直接填写固定名称，同名会自动加 v2、v3。", outputIds: []};
    }
    if (!/^[A-Za-z0-9_-]*$/.test(literal)) {
      return {ok: false, message: "替换后的新模板 ID 仅允许 ASCII 字母、数字、下划线和连字符；不能包含中文、空格或其他符号。", outputIds: []};
    }
    if (!literal) {
      return {ok: false, message: "替换后的新模板 ID 不能与原模板 ID 相同。", outputIds: []};
    }
    const sources = asArray(originalIds).map(String);
    const outputIds = sources.map((originalId) => rule.replace(token, originalId));
    for (let index = 0; index < outputIds.length; index += 1) {
      const outputId = outputIds[index];
      if (!/^[A-Za-z0-9_-]+$/.test(outputId)) {
        return {ok: false, message: "替换后的新模板 ID 仅允许 ASCII 字母、数字、下划线和连字符；不能包含中文、空格或其他符号。", outputIds: []};
      }
      if (outputId.length > 180) {
        return {ok: false, message: "替换后的新模板 ID 不得超过 180 字节。", outputIds: []};
      }
      if (outputId === sources[index]) {
        return {ok: false, message: "替换后的新模板 ID 不能与原模板 ID 相同。", outputIds: []};
      }
    }
    const reserved = new Set(outputIds.map(outputId => outputId.toLowerCase()));
    const assigned = new Set();
    for (let index = 0; index < outputIds.length; index += 1) {
      const base = outputIds[index];
      if (assigned.has(base.toLowerCase())) {
        let version = 2, candidate;
        do {
          const suffix = `v${version++}`;
          candidate = base.slice(0, 180 - suffix.length) + suffix;
        } while (reserved.has(candidate.toLowerCase()) || assigned.has(candidate.toLowerCase()) || candidate === sources[index]);
        outputIds[index] = candidate;
      }
      assigned.add(outputIds[index].toLowerCase());
    }
    return {ok: true, message: "", outputIds};
  }

  return {
    parseTemplateIds,
    searchHelpTopics,
    normalizeImported,
    describeImportInspection,
    normalizeTemplate,
    normalizeJob,
    tileMovementView,
    canRerunTemplate,
    namingRegenerationParameters,
    parameterAdviceView,
    validateRatioRangePercent,
    replenishRecommendation,
    replenishTypeWarning,
    summarizeCommitItems,
    interpretSyncReport,
    summarizeTemplateOutcomes,
    validateRetainedCounts,
    shouldAcceptJobResponse,
    normalizeSyncPlanEntries,
    normalizeHistoryRows,
    resolveCandidate,
    nextPollDelay,
    planFingerprint,
    validateRatioPercent,
    navigationScrollBehavior,
    resolveFlowStep,
    validateNamingRule,
  };
}));
