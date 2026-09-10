import { ParameterInput } from './DynamicParameters';
import type { Binding, ParameterRule, Value, Workflow } from './types';
import { OWNER_LABELS } from './types';
import { bindingKey, roleLabel, roleOwner } from './WorkflowBindings';

const FIELD_LABELS: Record<string, string> = {ckpt_name: '模型', unet_name: '扩散模型', clip_name: '文本编码模型', vae_name: 'VAE 模型', lora_name: 'LoRA 模型', sampler_name: '采样器', scheduler: '调度器', steps: '采样步数', cfg: '提示词强度', seed: '随机种子', filename_prefix: '输出文件名前缀'};
export function WorkflowParameterPanel({workflow, bindings, values, rules, search, onSearch, onValues, onRules, onBinding}: {workflow: Workflow; bindings: Record<string, Binding>; values: Record<string, Value>; rules: Record<string, ParameterRule>; search: string; onSearch: (value: string) => void; onValues: (values: Record<string, Value>) => void; onRules: (rules: Record<string, ParameterRule>) => void; onBinding: () => void}) {
  const filtered = workflow.parameters.filter(p => [p.key, p.class_type, workflow.workflow[p.node_id]?._meta?.title, FIELD_LABELS[p.field], values[p.key] ?? p.default].join(' ').toLowerCase().includes(search.toLowerCase().trim()));
  const nodes = [...new Set(filtered.map(p => p.node_id))];
  return <><h3>模型与参数</h3><p className="muted">直接修改此工作流中的实际值。标记为自动填写的字段会在生成时更新；创作时的高级覆盖仍优先。</p><div className="parameter-search"><label htmlFor="parameter-search">查找模型、参数或节点</label><input id="parameter-search" type="search" placeholder="例如：模型、sampler、137、图片文件名" value={search} onChange={event => onSearch(event.target.value)} /><span className="muted">显示 {filtered.length} / {workflow.parameters.length} 个字段</span>{search && <button type="button" onClick={() => onSearch('')}>清除搜索</button>}</div>
    {!filtered.length && <p className="notice">没有匹配字段。请尝试节点编号或清除搜索。</p>}
    {nodes.map(id => <section className="parameter-node" key={id}><div className="parameter-node-heading"><h4>{workflow.workflow[id]._meta?.title || workflow.workflow[id].class_type}</h4><small>{workflow.workflow[id].class_type} · 节点 {id}</small></div><div className="parameter-list">{filtered.filter(p => p.node_id === id).map(item => {
      const role = Object.entries(bindings).find(([, b]) => bindingKey(b) === item.key)?.[0];
      const owner = role ? roleOwner(role) : item.asset_kind ? 'asset_resolver' : rules[item.key]?.owner ?? 'workflow';
      const customized = Object.hasOwn(values, item.key);
      return <div className="field workflow-parameter" key={item.key} data-parameter={item.key}><div className="parameter-title"><label>{FIELD_LABELS[item.field] || item.field}</label><span className="parameter-source">{OWNER_LABELS[owner]}</span></div><small className="muted">{item.key}{role ? ` · ${roleLabel(role)}` : ''}</small>
        <ParameterInput parameter={item} label={`参数值 ${item.key}`} value={values[item.key] ?? workflow.workflow[item.node_id].inputs[item.field] as Value} onChange={value => onValues({...values, [item.key]: value})} />
        <div className="parameter-hint"><small>{owner !== 'workflow' ? '模板默认值；生成时自动填写或由用户覆盖' : customized ? '已修改默认值' : '使用导入值'}{item.min != null ? ` · 最小 ${item.min}` : ''}{item.max != null ? ` · 最大 ${item.max}` : ''}</small>{customized && <button type="button" onClick={() => { const next = {...values}; delete next[item.key]; onValues(next); }}>恢复导入值</button>}</div>
        <details className="parameter-policy"><summary>自动填写与覆盖规则</summary>{role ? <p>已用于{roleLabel(role)}。<button type="button" onClick={onBinding}>修改用途绑定</button></p> : item.asset_kind ? <p>图片等素材字段需要先指定用途。<button type="button" onClick={onBinding}>指定素材用途</button></p> : <label>由谁填写<select aria-label={`归属 ${item.key}`} value={owner} onChange={event => onRules({...rules, [item.key]: {...(rules[item.key] ?? {editable: true, override_policy: 'advanced'}), owner: event.target.value as 'ai' | 'workflow' | 'user'}})}><option value="workflow">使用工作流默认值</option><option value="ai">AI 自动生成</option><option value="user">用户填写</option></select></label>}
          <label className="checkbox"><input aria-label={`允许覆盖 ${item.key}`} type="checkbox" checked={(rules[item.key]?.override_policy ?? item.override_policy) === 'advanced' && (rules[item.key]?.editable ?? item.editable)} onChange={event => onRules({...rules, [item.key]: {...rules[item.key], editable: event.target.checked, override_policy: event.target.checked ? 'advanced' : 'never'}})} />允许创作时高级覆盖</label>
        </details>
      </div>;
    })}</div></section>)}
  </>;
}
