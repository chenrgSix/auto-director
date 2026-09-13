import { useState } from 'react';
import { api } from './api';
import type { Notify } from './App';
import type { Episode, QAPolicy } from './types';
import { ErrorNotice } from './ui';

export const QA_POLICIES = [
  { id: 'advisory', label: '后台复核（推荐）', detail: '每镜在后台检查一次，不等待模型即可继续渲染和导出。画面问题标为待复核，由你决定是否重做。' },
  { id: 'strict', label: '严格质检', detail: '检查关键帧和视频，等待结果后继续。画面不达标时自动重试；达到上限会暂停。' },
] as const;

export function EpisodeQAPolicy({ episode, busy, locked, toggleLocked, setBusy, onSaved, notify }: {
  episode: Episode; busy: boolean; locked: boolean; toggleLocked: boolean; setBusy: (value: boolean) => void;
  onSaved: () => void; notify: Notify;
}) {
  const policy = episode.qa_policy ?? 'strict';
  const enabled = episode.qa_enabled ?? true;
  const manual = episode.creation_visual_review === 'manual';
  const [draft, setDraft] = useState<{ qa_policy: QAPolicy; qa_enabled: boolean; expected_version: number }>();
  const [error, setError] = useState<string>();
  const canToggle = policy === 'advisory' && !toggleLocked;
  const current = QA_POLICIES.find(item => item.id === policy)!;
  async function save() {
    if (!draft) return;
    setBusy(true); setError(undefined);
    try {
      await api(`/episodes/${episode.id}/qa-policy`, 'PATCH', {
        ...draft, expected_version: canToggle && draft.qa_policy === policy ? episode.version : draft.expected_version,
      });
      setDraft(undefined); onSaved();
      notify(draft.qa_enabled ? '视觉复核设置已保存，作用于后续镜头' : 'AI 视觉复核已关闭，未完成复核已停止，已有素材和复核结果保留');
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="panel" aria-label="短片视觉复核设置">
    <div className="panel-title">AI 视觉复核 · {manual ? '人工复核' : enabled ? current.label : '已关闭'}</div>
    {manual ? <p className="muted">本片在创作包交付时选择了人工复核，不调用视觉模型。需要 AI 复核时，在创作包交付设置选择视觉模型。</p> : !draft ? <>
      <p className="muted">{enabled ? current.detail : '不调用视觉模型，仍检查视频文件、时长和拼接。'}</p>
      <button disabled={busy || (locked && !canToggle)} onClick={() => { setError(undefined); setDraft({ qa_policy: policy, qa_enabled: enabled, expected_version: episode.version }); }}>设置视觉复核</button>
    </> : <>
      <label className="checkbox"><input type="checkbox" checked={draft.qa_enabled} disabled={busy || (locked && !canToggle)} onChange={event => setDraft({ ...draft, qa_enabled: event.target.checked })} />启用 AI 视觉复核</label>
      <div className="field"><label htmlFor="episode-qa-policy">质检策略</label><select id="episode-qa-policy" disabled={busy || locked || !draft.qa_enabled} value={draft.qa_policy} onChange={event => setDraft({ ...draft, qa_policy: event.target.value as QAPolicy })}>
        {QA_POLICIES.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
      </select><small className="muted">{QA_POLICIES.find(item => item.id === draft.qa_policy)?.detail}</small></div>
      <p className="muted">建议式复核可在生成期间开关。关闭会停止排队及正在进行的复核；开启作用于后续镜头，旧视频不会自动重检。剧本、提示词、已有素材和历史复核结果保留。</p>
      <div className="actions"><button disabled={busy} onClick={() => setDraft(undefined)}>取消修改</button><button className="primary" disabled={busy || (locked && !canToggle) || (draft.qa_policy === policy && draft.qa_enabled === enabled)} onClick={() => void save()}>保存复核设置</button></div>
    </>}
    <p className="muted">开启后需要配置视觉模型。严格质检需停止生成并核对原任务后切换；关闭 AI 复核不会跳过媒体技术检查。</p>
    {error && <ErrorNotice>{error}</ErrorNotice>}
  </section>;
}
