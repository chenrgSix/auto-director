import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import type { Problem, Settings } from './types';

type Kind = 'director' | 'vision';
type Result = { kind: Kind; model: string; success: boolean; elapsed_seconds: number; timeout_seconds: number; checks: string[]; error: Problem | null };
const labels: Record<Kind, string> = { director: '导演模型', vision: '视觉模型' };

export function ModelTests({ settings, disabled, dirty, onPending }: { settings: Settings; disabled: boolean; dirty: boolean; onPending: (value: boolean) => void }) {
  const [results, setResults] = useState<Partial<Record<Kind, Result>>>({});
  const [pending, setPending] = useState<Kind>();
  const [error, setError] = useState('');
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => { active.current?.abort(); active.current = null; onPending(false); }, [onPending]);
  function cancel() { active.current?.abort(); active.current = null; setPending(undefined); onPending(false); setError('已取消测试等待'); }
  async function test(kind: Kind) {
    const controller = new AbortController(); active.current = controller;
    setPending(kind); onPending(true); setError(''); setResults(current => ({ ...current, [kind]: undefined }));
    try {
      const result = await api<Result>('/models/test', 'POST', { kind }, controller.signal);
      if (active.current === controller) setResults(current => ({ ...current, [kind]: result }));
    } catch (error) {
      if (active.current === controller && !controller.signal.aborted) setError((error as Error).message);
    } finally {
      if (active.current === controller) { active.current = null; setPending(undefined); onPending(false); }
    }
  }
  return <section className="model-tests" aria-label="模型测试">
    <div className="actions">{(['director', 'vision'] as const).map(kind => <button key={kind} type="button" disabled={disabled || dirty || !!pending || !(kind === 'director' ? settings.llm_model : settings.vlm_model)} onClick={() => void test(kind)}>{pending === kind ? `${labels[kind]}测试中…` : `测试${labels[kind]}`}</button>)}{pending && <button type="button" onClick={cancel}>取消模型测试</button>}</div>
    <p className="muted">测试使用已保存配置调用模型，检查 JSON 输出；视觉测试会附带一张小图。最多等待 120 秒。</p>
    {dirty && <p className="muted">先保存更改，再测试模型；旧测试结果已清除。</p>}
    {!settings.vlm_model && <p className="muted">配置视觉 QA 模型后可测试图像输入。</p>}
    {error && <div className="notice" role="status">{error}</div>}
    {Object.values(results).filter((result): result is Result => !!result).map(result => <div className={`notice ${result.success ? '' : 'error'}`} role={result.success ? 'status' : 'alert'} key={result.kind}>
      <div><strong className={result.success ? 'success-text' : undefined}>{labels[result.kind]}{result.success ? '测试通过' : '测试失败'} · {result.model || '未配置'} · {result.elapsed_seconds.toFixed(2)} 秒</strong>
      <p>{result.success ? result.kind === 'vision' ? 'JSON 输出与测试图识别通过。' : '已收到符合要求的 JSON 输出。' : result.error?.message}</p>
      {result.error && <small>{result.error.code}</small>}
      {result.success && <small>小请求通过不代表长任务或生成质量已通过验收。</small>}</div>
    </div>)}
  </section>;
}
