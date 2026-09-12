import { useState } from 'react';
import { api } from './api';
import type { Notify } from './App';
import type { Episode } from './types';
import { ErrorNotice } from './ui';

const MODES = [
  { id: 'fast', label: '快速探索', detail: '1 张首帧候选 · 默认重试预算 1 次' },
  { id: 'standard', label: '标准创作', detail: '1 张首帧候选 · 默认重试预算 2 次' },
  { id: 'high', label: '精细打磨', detail: '2 张首帧候选 · 更高画面标准 · 默认重试预算 3 次' },
] as const;

export function EpisodeQuality({ episode, busy, locked, setBusy, onSaved, notify }: {
  episode: Episode; busy: boolean; locked: boolean; setBusy: (value: boolean) => void;
  onSaved: () => void; notify: Notify;
}) {
  const [draft, setDraft] = useState<{ quality: string; expected_version: number }>();
  const [error, setError] = useState<string>();
  const current = MODES.find(mode => mode.id === episode.quality);
  const advisory = (episode.qa_policy ?? 'strict') === 'advisory';
  async function save() {
    if (!draft) return;
    setBusy(true); setError(undefined);
    try {
      await api(`/episodes/${episode.id}/quality`, 'PATCH', draft);
      setDraft(undefined); onSaved();
      notify('质量模式已保存，剧本和已有素材已保留，尚未开始生成');
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="panel" aria-label="短片质量模式">
    <div className="panel-title">生成质量 · {current?.label || episode.quality}</div>
    {!draft ? <>
      <p className="muted">可以保留现有剧本和素材，切换后续生成的质量模式。</p>
      <button disabled={busy || locked} onClick={() => { setError(undefined); setDraft({ quality: episode.quality, expected_version: episode.version }); }}>切换质量模式</button>
    </> : <>
      <div className="field"><label htmlFor="episode-quality">质量模式</label><select id="episode-quality" disabled={busy || locked} value={draft.quality} onChange={event => setDraft({ ...draft, quality: event.target.value })}>
        {MODES.map(mode => <option key={mode.id} value={mode.id}>{mode.label}{mode.id === 'standard' ? '（推荐）' : ''}</option>)}
      </select><small className="muted">{draft.quality === 'high' && advisory ? '1 张首帧 · 更高画面标准 · 默认重试预算 3 次' : MODES.find(mode => mode.id === draft.quality)?.detail}</small></div>
      <p className="muted">切换后使用所选模式的默认重试预算。剧本、提示词、分镜时长和已生成素材保留，已有画面尺寸沿用。{advisory ? '当前先完成整片，画面评分不触发自动重画，重试预算只用于可恢复的生成问题。' : '当前严格质检，画面不达标时也使用此预算重试。'}候选比较与视觉质检需要启用视觉检查并配置视觉模型。</p>
      <p className="muted">{episode.status === 'COMPLETED' ? '已完成的视频保持原样，需要重做时使用「重新生成」。' : episode.preview_required && !episode.preview_approved_at ? '保存后继续审阅分镜，确认后点击「开始视频生成」。' : '保存后点击「继续生成」补齐缺失部分；已有结果需要重做时使用「重新生成」。'}</p>
      <div className="actions"><button disabled={busy} onClick={() => setDraft(undefined)}>取消修改</button><button className="primary" disabled={busy || locked || draft.quality === episode.quality} onClick={() => void save()}>保存质量模式</button></div>
    </>}
    {locked && <p className="muted">请先等待任务停止并核对未完成作业，再切换质量模式。</p>}
    {error && <ErrorNotice>{error}</ErrorNotice>}
  </section>;
}
