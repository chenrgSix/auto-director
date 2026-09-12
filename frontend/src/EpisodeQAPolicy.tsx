import { useState } from 'react';
import { api } from './api';
import type { Notify } from './App';
import type { Episode, QAPolicy } from './types';
import { ErrorNotice } from './ui';

export const QA_POLICIES = [
  { id: 'advisory', label: '先完成整片（推荐）', detail: '每段视频生成后检查一次。画面问题标为待复核，继续完成整片，再由你选择镜头重做。' },
  { id: 'strict', label: '严格质检', detail: '检查关键帧和视频，画面不达标时自动重试；达到重试上限会暂停，等待处理。' },
] as const;

export function EpisodeQAPolicy({ episode, busy, locked, setBusy, onSaved, notify }: {
  episode: Episode; busy: boolean; locked: boolean; setBusy: (value: boolean) => void;
  onSaved: () => void; notify: Notify;
}) {
  const policy = episode.qa_policy ?? 'strict';
  const [draft, setDraft] = useState<{ qa_policy: QAPolicy; expected_version: number }>();
  const [error, setError] = useState<string>();
  const current = QA_POLICIES.find(item => item.id === policy)!;
  async function save() {
    if (!draft) return;
    setBusy(true); setError(undefined);
    try {
      await api(`/episodes/${episode.id}/qa-policy`, 'PATCH', draft);
      setDraft(undefined); onSaved();
      notify('质检策略已保存，剧本和已有素材已保留，尚未开始生成');
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="panel" aria-label="短片质检策略">
    <div className="panel-title">质检策略 · {current.label}</div>
    {!draft ? <>
      <p className="muted">{current.detail}</p>
      <button disabled={busy || locked} onClick={() => { setError(undefined); setDraft({ qa_policy: policy, expected_version: episode.version }); }}>切换质检策略</button>
    </> : <>
      <div className="field"><label htmlFor="episode-qa-policy">质检策略</label><select id="episode-qa-policy" disabled={busy || locked} value={draft.qa_policy} onChange={event => setDraft({ ...draft, qa_policy: event.target.value as QAPolicy })}>
        {QA_POLICIES.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
      </select><small className="muted">{QA_POLICIES.find(item => item.id === draft.qa_policy)?.detail}</small></div>
      <p className="muted">保存只影响后续质检，保留剧本、提示词和已有素材。{episode.status === 'COMPLETED' ? '已完成的视频保持原样，需要调整时选择镜头重跑。' : episode.preview_required && !episode.preview_approved_at ? '确认分镜后点击「开始视频生成」。' : '保存后点击「继续生成」补齐剩余镜头。'}</p>
      <div className="actions"><button disabled={busy} onClick={() => setDraft(undefined)}>取消修改</button><button className="primary" disabled={busy || locked || draft.qa_policy === policy} onClick={() => void save()}>保存质检策略</button></div>
    </>}
    <p className="muted">视觉检查需启用并配置视觉模型。连接中断、生成失败和不可用的视频仍会保留进度并暂停，避免丢失结果或重复提交。</p>
    {locked && <p className="muted">请先等待任务停止、页面重新连接并核对未完成作业，再切换质检策略。</p>}
    {error && <ErrorNotice>{error}</ErrorNotice>}
  </section>;
}
