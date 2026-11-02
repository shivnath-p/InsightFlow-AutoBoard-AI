import React, { useEffect, useRef } from 'react';
import * as echarts from 'echarts/core';
import { BarChart, LineChart, PieChart } from 'echarts/charts';
import { GridComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';

// Tree-shaken ECharts: only the chart types the dashboards use.
echarts.use([BarChart, LineChart, PieChart, GridComponent, TooltipComponent, CanvasRenderer]);

const labelOf = (row) => Object.values(row.labels).join(' / ') || 'Total';

// Renders one fact (verified snapshot) as a chart. Data never comes from live queries.
export default function Chart({ type, fact, height = 260 }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !fact) return undefined;
    const chart = echarts.init(ref.current);
    const rows = fact.rows;
    const names = rows.map(labelOf);
    const values = rows.map((r) => r.value);
    const option = type === 'pie'
      ? { tooltip: { trigger: 'item' },
          series: [{ type: 'pie', radius: '65%', data: rows.map((r) => ({ name: labelOf(r), value: r.value })) }] }
      : { tooltip: { trigger: 'axis' },
          grid: { left: 48, right: 16, top: 24, bottom: 48 },
          xAxis: { type: 'category', data: names, axisLabel: { rotate: names.length > 6 ? 30 : 0 } },
          yAxis: { type: 'value' },
          series: [{ type: type === 'line' ? 'line' : 'bar', data: values, smooth: type === 'line' }] };
    chart.setOption(option);
    const onResize = () => chart.resize();
    window.addEventListener('resize', onResize);
    return () => {
      window.removeEventListener('resize', onResize);
      chart.dispose();
    };
  }, [type, fact]);
  return <div ref={ref} style={{ width: '100%', height }} role="img" aria-label={fact?.description} />;
}

export function Kpi({ fact }) {
  const v = fact?.rows?.[0]?.value;
  return <div className="kpi"><strong>{v == null ? 'n/a' : Number(v).toLocaleString()}</strong></div>;
}

export function FactTable({ fact }) {
  return (
    <table>
      <thead><tr><th>Group</th><th>Value</th><th>Share</th></tr></thead>
      <tbody>
        {fact.rows.map((r, i) => (
          <tr key={i}>
            <td>{labelOf(r)}</td>
            <td>{r.value == null ? 'n/a' : Number(r.value).toLocaleString()}</td>
            <td>{r.share_pct != null ? `${r.share_pct}%` : ''}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
