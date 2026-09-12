import { useEffect, useRef, useState } from 'react';
import { Sparkles } from 'lucide-react';
import { api } from './api';
import type { Notify } from './App';
import type { Episode, PreviewPromptField, PromptOptimizationProposal, Shot } from './types';
import { ErrorNotice } from './ui';

const LABELS: Record<PreviewPromptField, string> = { start_frame_prompt: '首帧提示词', end_frame_prompt: '尾帧提示词', video_prompt: '视频提示词' };

export function PromptOptimization({ episode, shot, locked, busy, setBusy, refresh, notify }: {
  episode: Episode; shot: Shot; locked: boolean; busy: boolean; setBusy: (value: boolean) => void;
  refresh: () => void; notify: Notify;
}) {
  const [open, setOpen] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [proposal, setProposal] = useState<PromptOptimizationProposal>();
  const [error, setError] = useState<string>();
  const [analyzing, setAnalyzing] = useState(false);
  const request = useRef<AbortController | null>(null);
  const lookup = useRef<AbortController | null>(null);
  const base = `/episodes/${episode.id}/shots/${shot.id}/prompt-optimization`;
  useEffect(() => {
    const controller = new AbortController();
    lookup.current = controller;
    void api<PromptOptimizationProposal | null>(base, 'GET', undefined, controller.signal)
      .then(value => { if (!controller.signal.aborted && value) setProposal(value); })
      .catch(() => { /* A proposal is optional; a failed lookup must not block playback. */ });
    return () => {
      controller.abort();
      if (request.current) { request.current.abort(); setBusy(false); }
    };
  }, [base, setBusy]);
  const stale = !!proposal && proposal.source_version !== episode.version;
  const affected = episode.shots.filter(item => proposal?.affected_shot_ids.includes(item.id));

  async function analyze() {
    lookup.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setBusy(true); setAnalyzing(true); setError(undefined);
    try {
      const result = await api<PromptOptimizationProposal>(base, 'POST', { expected_version: episode.version, feedback }, controller.signal);
      if (!controller.signal.aborted) setProposal(result);
    } catch (error) {
      if (!controller.signal.aborted) setError((error as Error).message);
    } finally {
      if (request.current === controller) { request.current = null; setAnalyzing(false); setBusy(false); }
    }
  }
  async function apply() {
    if (!proposal || stale) return;
    setBusy(true); setError(undefined);
    try {
      await api(`/episodes/${episode.id}/prompt-optimizations/${proposal.id}/apply`, 'POST', { expected_version: proposal.source_version });
      setProposal(undefined); setOpen(false); refresh();
      notify('已按优化方案安排修正，旧视频与提示词已保留');
    } catch (error) { setError((error as Error).message); refresh(); }
    finally { setBusy(false); }
  }

  return <section className="panel prompt-optimization" aria-label="AI 提示词优化">
    <div className="panel-title"><strong>改善这个镜头</strong><button disabled={busy} onClick={() => setOpen(!open)}><Sparkles size={15} />{open ? '收起优化' : 'AI 优化提示词'}</button></div>
    <p className="muted">结合实际画面和复核问题，先看修改方案，再决定是否重跑。</p>
    {open && <div className="optimization-content">
      <label>你希望改善什么（可选）<textarea rows={2} maxLength={1000} value={feedback} disabled={busy || locked} onChange={e => setFeedback(e.target.value)} placeholder="例如：保留当前构图，让角色真正向前走" /></label>
      <div className="actions"><button disabled={locked || busy} onClick={() => void analyze()}>{analyzing ? '正在观察画面并优化…' : proposal ? '重新分析并预览' : '分析并预览修改'}</button>{analyzing && <button onClick={() => request.current?.abort()}>取消分析</button>}</div>
      {analyzing && <p className="muted" role="status">正在检查按时间顺序抽取的画面及输入关键帧。这里只生成建议，不开始视频生成。</p>}
      {error && <ErrorNotice>{error}</ErrorNotice>}
      {proposal && <div className="optimization-proposal">
        {stale && <div className="notice" role="status">短片已更新，或这个方案已执行。以下是旧版建议，请重新分析后再确认。</div>}
        <h3>{proposal.decision === 'revise' ? '建议定向修正' : proposal.decision === 'keep' ? '建议保留当前镜头' : '建议人工调整'}</h3>
        <p>{proposal.summary}</p>
        <p className="muted">判断把握：{{ high: '较高', medium: '中等', low: '不足' }[proposal.confidence]}。{proposal.limitations}</p>
        {(Object.entries(proposal.changes) as [PreviewPromptField, { prompt: string; reason: string }][]).map(([field, change]) => <div className="optimization-change" key={field}>
          <h4>{LABELS[field]}</h4><p>{change.reason}</p><div className="optimization-comparison"><div><strong>修改前</strong><pre>{proposal.original_prompts[field]}</pre></div><div><strong>修改后</strong><pre>{change.prompt}</pre></div></div>
        </div>)}
        {proposal.decision === 'revise' && <>
          <div className="notice">本镜重做：{proposal.frames.length ? `${proposal.frames.map(role => role === 'start_frame' ? '首帧' : '尾帧').join('、')}及视频` : '仅视频，复用已有关键帧'}。受影响镜头：{affected.map(item => `第 ${item.index + 1} 镜`).join('、')}。{affected.length > 1 && '后续连续镜头需重新衔接。'}完成后重新合成整片。</div>
          <p className="muted">故事与时长保持，旧视频可在重跑历史查看。本次执行一轮修正，画面仍有问题会保留提示；模型效果不保证一定改善。</p>
          <button className="primary" disabled={locked || busy || stale} onClick={() => void apply()}>确认优化并重跑</button>
        </>}
        {!!Object.keys(proposal.locked_fields).length && <details><summary>当前不可自动修改的提示词</summary>{Object.entries(proposal.locked_fields).map(([field, reason]) => <p key={field}>{LABELS[field as PreviewPromptField]}：{reason}</p>)}</details>}
      </div>}
      <small className="muted">AI 查看采样图片，不能完整判断采样间的动作，也不检查音频。高级固定覆盖始终保持。</small>
    </div>}
  </section>;
}
