import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import type { Binding, WorkflowCapability } from './types';
import { CAPABILITY_LABELS } from './types';
import { roleLabel } from './WorkflowBindings';

export type Recognition = {
  capability: WorkflowCapability; bindings: Record<string, Binding>; outputs: Record<string, string>;
  reasons: Record<string, string>; notes: string[];
};

export function mergeBindings(existing: Record<string, Binding>, proposed: Record<string, Binding>) {
  const merged = {...existing};
  const occupied = new Set(Object.values(existing).map(b => `${b.node_id}.${b.input}`));
  for (const [role, binding] of Object.entries(proposed)) {
    const key = `${binding.node_id}.${binding.input}`;
    if (!merged[role] && !occupied.has(key)) { merged[role] = binding; occupied.add(key); }
  }
  return merged;
}

export function WorkflowAI({graph, capability, disabled, onApply}: {
  graph: Record<string, unknown>; capability?: WorkflowCapability; disabled?: boolean;
  onApply: (result: Recognition) => void;
}) {
  const [result, setResult] = useState<Recognition>();
  const [error, setError] = useState('');
  const [pending, setPending] = useState(false);
  const active = useRef<AbortController | null>(null);
  // The parent keys this component by file/workflow and purpose; stale requests are cancelled.
  useEffect(() => () => { active.current?.abort(); active.current = null; }, []);
  function cancel() { active.current?.abort(); active.current = null; setPending(false); setError(''); }
  async function recognize() {
    cancel();
    const controller = new AbortController(); active.current = controller;
    setPending(true); setResult(undefined); setError('');
    try {
      const proposal = await api<Recognition>('/workflows/analyze', 'POST', {workflow: graph, ...(capability ? {capability} : {})}, controller.signal);
      if (active.current === controller) setResult(proposal);
    } catch (failure) {
      if (active.current === controller && !controller.signal.aborted) setError((failure as Error).message);
    } finally {
      if (active.current === controller) { active.current = null; setPending(false); }
    }
  }
  return <section className="workflow-ai" aria-label="AI 识别绑定">
    <div className="actions"><button disabled={disabled || pending} onClick={() => void recognize()}>{pending ? 'AI 正在识别…' : 'AI 识别（可选）'}</button>{pending && <button onClick={cancel}>取消识别</button>}</div>
    <p className="muted">使用“连接与设置”中的导演模型分析工作流。你可以直接导入或手动配置；识别最长等待 45 秒。</p>
    {error && <div className="notice" role="alert">{error}。仍可直接导入或手动绑定。</div>}
    {result && <div className="ai-proposal">
      <strong>AI 建议 · {CAPABILITY_LABELS[result.capability]}</strong>
      <ul>{Object.entries(result.bindings).map(([role, binding]) => <li key={role}><strong>{roleLabel(role)}</strong> → {binding.node_id}.{binding.input}<small>{result.reasons[role]}{binding.transform === 'duration_to_frames' && ` · 帧数规则 ${binding.frame_multiple}n+${binding.frame_offset}`}</small></li>)}{Object.entries(result.outputs).map(([media, node]) => <li key={media}><strong>生成结果</strong> → {node}<small>{result.reasons[`output:${media}`]}</small></li>)}</ul>
      {result.notes.map((note, index) => <p key={index} className="muted">{note}</p>)}
      <button disabled={disabled} onClick={() => { onApply(result); setResult(undefined); }}>应用建议（保留已有绑定）</button>
    </div>}
  </section>;
}
