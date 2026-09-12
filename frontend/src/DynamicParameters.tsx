import type { Asset, Parameter, Value, Workflow } from './types';
import { OWNER_LABELS } from './types';
import { api } from './api';
import type { Notify } from './App';

export function ParameterInput({ parameter, value, onChange, label }: { parameter: Parameter; value: Value; label?: string; onChange: (value: Value) => void }) {
  if (parameter.type === 'boolean') return <input aria-label={label ?? parameter.key} type="checkbox" checked={Boolean(value)} onChange={event => onChange(event.target.checked)} />;
  if (parameter.enum?.length) return <select aria-label={label ?? parameter.key} value={String(value)} onChange={event => { const selected = parameter.enum!.find(item => String(item) === event.target.value); if (selected !== undefined) onChange(selected); }}>{!parameter.enum.includes(value) && <option value={String(value)}>{String(value)}（当前不可用）</option>}{parameter.enum.map(item => <option key={String(item)} value={String(item)}>{String(item)}</option>)}</select>;
  if (parameter.type === 'textarea' || parameter.field === 'text') return <textarea aria-label={label ?? parameter.key} value={String(value)} onChange={event => onChange(event.target.value)} rows={3} />;
  const numeric = ['integer', 'number'].includes(parameter.type);
  return <input aria-label={label ?? parameter.key} type={numeric ? 'number' : 'text'} value={String(value)} min={parameter.min ?? undefined} max={parameter.max ?? undefined} step={parameter.step ?? (parameter.type === 'integer' ? 1 : 'any')} onChange={event => onChange(numeric ? Number(event.target.value) : event.target.value)} />;
}

function ConstraintDescription({ parameter }: { parameter: Parameter }) {
  const kinds: Record<string, string> = { integer: '整数', number: '数值', boolean: '布尔值', text: '文本', textarea: '文本', select: '选项' };
  const limits = [kinds[parameter.type]];
  if (parameter.min != null) limits.push(`最小 ${parameter.min}`);
  if (parameter.max != null) limits.push(`最大 ${parameter.max}`);
  if (parameter.step != null && parameter.step > 0) limits.push(`步长 ${parameter.step}（从 ${parameter.min ?? 0} 起）`);
  return <><span>{limits.filter(Boolean).join(' · ') || '未提供额外限制'}</span>{!!parameter.enum?.length && <details className="parameter-enum"><summary>可选值（{parameter.enum.length} 项）</summary><p>{parameter.enum.map(value => JSON.stringify(value)).join('、')}</p></details>}</>;
}

export function ParameterConstraints({ parameter }: { parameter: Parameter }) {
  const downstream = parameter.downstream_constraints ?? [];
  return <div className="parameter-constraints"><div>本字段：<ConstraintDescription parameter={parameter} /></div>{downstream.length > 0 && <details className="parameter-consumers" open={downstream.length <= 3}><summary>还需满足 {downstream.length} 个下游输入限制</summary><p>同一个值会传入以下节点，必须分别满足各项限制；高级覆盖也适用。</p><ul>{downstream.map(consumer => <li key={consumer.key}><strong>{consumer.class_type} · 节点 {consumer.node_id}.{consumer.field}</strong><ConstraintDescription parameter={consumer} /></li>)}</ul></details>}</div>;
}


export function ParameterOverrides({ workflow, values, onChange, notify }: { workflow: Workflow; values: Record<string, Value>; onChange: (values: Record<string, Value>) => void; notify: Notify }) {
  async function upload(key: string, file?: File) {
    if (!file) return;
    const data = new FormData(); data.append('file', file);
    try { const asset = await api<Asset>('/assets', 'POST', data); onChange({ ...values, [key]: asset.id }); notify('覆盖素材已上传'); }
    catch (error) { notify((error as Error).message, true); }
  }
  return <section className="override-group"><h3>{workflow.name}</h3><p className="muted">未勾选的参数继续自动计算或继承工作流。覆盖值仍受能力和显存约束。</p>{workflow.parameters.filter(item => item.editable && item.override_policy === 'advanced').map(item => {
    const selected = Object.hasOwn(values, item.key);
    return <div className="override-field" key={item.key}><label className="checkbox"><input aria-label={`覆盖 ${workflow.name} / ${item.key}`} type="checkbox" checked={selected} onChange={event => { const next = { ...values }; if (event.target.checked) next[item.key] = item.owner === 'asset_resolver' ? '' : workflow.parameter_values[item.key] ?? item.default; else delete next[item.key]; onChange(next); }} />{item.field}<small>{OWNER_LABELS[item.owner]} · {item.key}</small></label>
      {selected && (item.owner === 'asset_resolver' ? <label className="file-field"><span>{values[item.key] ? '已选择上传素材' : '请选择实际素材'}</span><input aria-label={`素材 ${workflow.name} / ${item.key}`} type="file" accept={item.role === 'reference_video' ? 'video/*' : item.role === 'reference_audio' ? 'audio/*' : 'image/png,image/jpeg,image/webp'} onChange={event => void upload(item.key, event.target.files?.[0])} /></label> : <ParameterInput parameter={item} label={`${workflow.name} / ${item.key}`} value={values[item.key]} onChange={value => onChange({ ...values, [item.key]: value })} />)}
      {selected && item.owner !== 'asset_resolver' && <ParameterConstraints parameter={item} />}
      {selected && item.owner === 'director' && <small>每镜固定时长须整除总时长。帧数参数按工作流 FPS 和帧数偏移换算。</small>}
    </div>;
  })}</section>;
}
