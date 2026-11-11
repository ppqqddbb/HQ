/* ECharts 主题与常用图表构造器 */
const THEME = {
  color: ["#38bdf8", "#22d3ee", "#34d399", "#a78bfa", "#fbbf24", "#f472b6", "#f87171", "#60a5fa"],
  textStyle: { color: "#9db0c9", fontFamily: "'Segoe UI','Microsoft YaHei',sans-serif" },
  grid: { left: 48, right: 20, top: 34, bottom: 34, containLabel: true },
  tooltip: {
    backgroundColor: "rgba(17,28,46,.96)",
    borderColor: "#2a3d5c",
    textStyle: { color: "#e8eef7", fontSize: 12 },
    extraCssText: "box-shadow:0 8px 24px rgba(0,0,0,.4);border-radius:8px;",
  },
};

const AXIS = {
  axisLine: { lineStyle: { color: "#243754" } },
  axisLabel: { color: "#8fa3bd", fontSize: 11 },
  splitLine: { lineStyle: { color: "rgba(36,55,84,.6)" } },
  axisTick: { show: false },
};

const CHARTS = [];

function mkChart(dom, option, opts = {}) {
  const c = echarts.init(dom, null, { renderer: "canvas" });
  c.setOption(Object.assign({ backgroundColor: "transparent", color: THEME.color }, option));
  CHARTS.push(c);
  const ro = new ResizeObserver(() => c.resize());
  ro.observe(dom);
  return c;
}

function disposeCharts() {
  while (CHARTS.length) { try { CHARTS.pop().dispose(); } catch (e) { } }
}

function chartBar(dom, labels, values, opts = {}) {
  // 支持分组柱状图：opts.series = [{name, data, color}] 时按多组绘制。
  const groups = opts.series && opts.series.length
    ? opts.series.map((s, i) => ({
      name: s.name, data: s.data,
      color: s.color || THEME.color[i % THEME.color.length],
      color2: s.color2,
    }))
    : [{ name: opts.name || "", data: values, color: opts.color || "#38bdf8", color2: opts.color2 }];
  const mkColor = (g) => (opts.gradient !== false
    ? new echarts.graphic.LinearGradient(0, 0, 0, 1, [
      { offset: 0, color: g.color }, { offset: 1, color: g.color2 || "rgba(56,189,248,.18)" }])
    : g.color);
  return mkChart(dom, {
    tooltip: Object.assign({ trigger: "axis", axisPointer: { type: "shadow" } }, THEME.tooltip),
    legend: groups.length > 1 ? {
      textStyle: { color: "#9db0c9", fontSize: 11 }, top: 0, right: 10,
      icon: "roundRect", itemWidth: 9, itemHeight: 9,
    } : undefined,
    grid: Object.assign({}, THEME.grid, { bottom: opts.rotate ? 62 : 34, top: groups.length > 1 ? 40 : 34 }),
    xAxis: {
      type: "category", data: labels, ...AXIS,
      axisLabel: Object.assign({}, AXIS.axisLabel, {
        rotate: opts.rotate ? 38 : 0,
        interval: opts.axisInterval !== undefined ? opts.axisInterval : (labels.length > 16 ? "auto" : 0),
        formatter: opts.axisFmt || undefined,
      }),
    },
    yAxis: Object.assign({ type: "value", ...AXIS, name: opts.yName || "", nameTextStyle: { color: "#6f839c", fontSize: 10 } }, opts.yOpt || {}),
    series: groups.map(g => ({
      name: g.name, type: "bar", data: g.data, barMaxWidth: groups.length > 1 ? 18 : 26,
      itemStyle: { borderRadius: [3, 3, 0, 0], color: mkColor(g) },
      label: opts.showLabel ? {
        show: true, position: "top", color: "#9db0c9", fontSize: 10,
        formatter: p => (opts.labelFmt ? opts.labelFmt(p.value) : String(p.value)),
      } : undefined,
    })),
  }, opts);
}

function chartLine(dom, x, series, opts = {}) {
  return mkChart(dom, {
    tooltip: Object.assign({ trigger: "axis" }, THEME.tooltip),
    legend: series.length > 1 ? { textStyle: { color: "#9db0c9", fontSize: 11 }, top: 0, right: 10, icon: "roundRect", itemWidth: 9, itemHeight: 9 } : undefined,
    grid: Object.assign({}, THEME.grid, opts.grid || {}),
    xAxis: Object.assign({ type: "category", data: x, boundaryGap: opts.boundaryGap !== false, ...AXIS }, opts.xOpt || {}),
    yAxis: Object.assign({ type: "value", ...AXIS, name: opts.yName || "", nameTextStyle: { color: "#6f839c", fontSize: 10 } }, opts.yOpt || {}),
    series: series.map((s, i) => ({
      name: s.name, type: s.type || "line", data: s.data,
      smooth: s.smooth !== false && (s.type || "line") === "line",
      showSymbol: s.showSymbol !== false && (s.data || []).length < 60,
      symbolSize: 4,
      lineStyle: { width: s.width || 2 },
      areaStyle: s.area ? {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: (s.color || THEME.color[i]) + "55" },
          { offset: 1, color: "rgba(0,0,0,0)" }]),
      } : undefined,
      barMaxWidth: 18,
      itemStyle: s.color ? { color: s.color } : undefined,
      yAxisIndex: s.yAxisIndex || 0,
      stack: s.stack,
    })),
  }, opts);
}

function chartPie(dom, items, opts = {}) {
  return mkChart(dom, {
    tooltip: Object.assign({ trigger: "item" }, THEME.tooltip),
    legend: { type: "scroll", orient: "vertical", right: 4, top: "center",
      textStyle: { color: "#9db0c9", fontSize: 11 }, itemWidth: 9, itemHeight: 9 },
    series: [{
      type: "pie", radius: ["46%", "72%"], center: ["38%", "52%"],
      avoidLabelOverlap: true, itemStyle: { borderColor: "#0f1a2b", borderWidth: 2 },
      label: { show: false },
      emphasis: { label: { show: true, fontSize: 12, color: "#e8eef7", formatter: "{b}\n{d}%" } },
      data: items,
    }],
  }, opts);
}

function chartRose(dom, labels, values, opts = {}) {
  return mkChart(dom, {
    tooltip: Object.assign({ trigger: "item" }, THEME.tooltip),
    series: [{
      type: "pie", roseType: "area", radius: ["18%", "78%"],
      startAngle: 90, clockwise: true,
      itemStyle: { borderColor: "#0f1a2b", borderWidth: 1 },
      label: { color: "#8fa3bd", fontSize: 10, formatter: "{b}°" },
      labelLine: { lineStyle: { color: "#2a3d5c" }, length: 6, length2: 6 },
      data: labels.map((l, i) => ({ name: l, value: values[i] })),
    }],
  }, opts);
}

function chartHeatmap(dom, xs, ys, data, opts = {}) {
  // 统一数据形式：ECharts 的 heatmap 只接受扁平的 [x, y, value] 列表。
  // 传入二维矩阵（按行嵌套）时必须先摊平，否则每行会被当成一个数据项，
  // 导致 value 为 undefined → visualMap 取不到颜色 → addColorStop 报错。
  let items = data;
  if (Array.isArray(items) && items.length && Array.isArray(items[0]) && Array.isArray(items[0][0])) {
    items = items.flat();
  }
  items = items.map(d => [d[0], d[1], Number(d[2])]);
  // 自动确定色阶范围：必须真正包住数据的 min/max，否则同样会算出 undefined 颜色。
  const vals = items.map(d => d[2]).filter(v => Number.isFinite(v));
  const dMin = vals.length ? Math.min(...vals) : 0;
  const dMax = vals.length ? Math.max(...vals) : 1;
  const min = opts.min !== undefined ? opts.min : Math.min(0, dMin);
  const max = opts.max !== undefined ? opts.max : Math.max(1e-9, dMax);
  return mkChart(dom, {
    tooltip: Object.assign({
      position: "top",
      formatter: p => `${ys[p.value[1]]} · ${xs[p.value[0]]}<br/><b>${opts.fmt ? opts.fmt(p.value[2]) : p.value[2]}</b>`,
    }, THEME.tooltip),
    grid: Object.assign({}, THEME.grid, { left: 60, bottom: 54, top: 20 }, opts.grid || {}),
    xAxis: Object.assign({ type: "category", data: xs, splitArea: { show: true }, ...AXIS }, opts.xOpt || {}),
    yAxis: Object.assign({ type: "category", data: ys, splitArea: { show: true }, ...AXIS }, opts.yOpt || {}),
    visualMap: {
      min, max,
      calculable: true, orient: "horizontal", left: "center", bottom: 2,
      itemWidth: 10, itemHeight: 70, textStyle: { color: "#8fa3bd", fontSize: 10 },
      formatter: opts.mapFmt,
      inRange: { color: opts.colors || ["#0c1d33", "#12507a", "#1e88b8", "#38bdf8", "#7ef0ff"] },
    },
    series: [{
      type: "heatmap", data: items, label: { show: opts.showLabel === true, fontSize: 9, color: opts.labelColor || "#dbe7f5" },
      itemStyle: { borderColor: "rgba(11,18,32,.5)", borderWidth: 1 },
      emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(56,189,248,.5)" } },
    }],
  }, opts);
}

function chartScatter(dom, series, opts = {}) {
  return mkChart(dom, {
    tooltip: Object.assign({ trigger: "item" }, THEME.tooltip),
    legend: { textStyle: { color: "#9db0c9", fontSize: 11 }, top: 0, right: 8,
      icon: "circle", itemWidth: 8, itemHeight: 8 },
    grid: Object.assign({}, THEME.grid, opts.grid || {}),
    xAxis: Object.assign({ type: "value", scale: true, ...AXIS,
      name: opts.xName || "", nameTextStyle: { color: "#6f839c", fontSize: 10 } }, opts.xOpt || {}),
    yAxis: Object.assign({ type: "value", scale: true, ...AXIS,
      name: opts.yName || "", nameTextStyle: { color: "#6f839c", fontSize: 10 } }, opts.yOpt || {}),
    series: series.map(s => ({
      name: s.name, type: "scatter", data: s.data,
      symbolSize: s.size || 6, itemStyle: { opacity: s.opacity ?? 0.72, color: s.color },
      large: s.data.length > 3000, largeThreshold: 2000,
      emphasis: { itemStyle: { opacity: 0.95 } },
    })),
  }, opts);
}

function chartGauge(dom, value, opts = {}) {
  return mkChart(dom, {
    series: [{
      type: "gauge", startAngle: 205, endAngle: -25, radius: "92%",
      min: 0, max: opts.max || 100, splitNumber: 5,
      axisLine: { lineStyle: { width: 9, color: [[0.4, "#f87171"], [0.7, "#fbbf24"], [1, "#34d399"]] } },
      pointer: { width: 4, itemStyle: { color: "#e8eef7" } },
      axisTick: { show: false },
      splitLine: { length: 8, lineStyle: { color: "#243754", width: 1 } },
      axisLabel: { color: "#6f839c", fontSize: 9, distance: 12 },
      detail: {
        valueAnimation: true, fontSize: 22, offsetCenter: [0, "42%"],
        color: "#e8eef7", formatter: v => v.toFixed(1),
      },
      title: { offsetCenter: [0, "68%"], color: "#6f839c", fontSize: 11 },
      data: [{ value, name: opts.name || "" }],
    }],
  }, opts);
}

function chartRadar(dom, indicators, series, opts = {}) {
  return mkChart(dom, {
    tooltip: THEME.tooltip,
    legend: { textStyle: { color: "#9db0c9", fontSize: 11 }, top: 0, icon: "roundRect", itemWidth: 9, itemHeight: 9 },
    radar: {
      indicator: indicators,
      radius: "66%", center: ["50%", "56%"],
      axisName: { color: "#8fa3bd", fontSize: 10 },
      splitLine: { lineStyle: { color: "rgba(36,55,84,.9)" } },
      splitArea: { areaStyle: { color: ["rgba(17,28,46,.3)", "rgba(21,34,56,.3)"] } },
      axisLine: { lineStyle: { color: "rgba(36,55,84,.9)" } },
    },
    series: [{
      type: "radar",
      data: series.map(s => ({
        value: s.value, name: s.name,
        areaStyle: { opacity: 0.22 },
        lineStyle: { width: 2 },
        symbolSize: 3,
      })),
    }],
  }, opts);
}

window.THEME = THEME; window.AXIS = AXIS;
window.mkChart = mkChart; window.disposeCharts = disposeCharts;
window.chartBar = chartBar; window.chartLine = chartLine; window.chartPie = chartPie;
window.chartRose = chartRose; window.chartHeatmap = chartHeatmap;
window.chartScatter = chartScatter; window.chartGauge = chartGauge; window.chartRadar = chartRadar;
