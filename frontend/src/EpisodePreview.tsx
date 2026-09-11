import { useState, type FormEvent } from 'react';
import { Check, Play, Save } from 'lucide-react';
import { api } from './api';
import type { Notify } from './App';
import type { Episode, PreviewPromptField, PreviewShotEdit } from './types';
import { ErrorNotice } from './ui';

function editableShots(episode: Episode): PreviewShotEdit[] {
  return episode.shots.map(shot => ({
    id: shot.id, title: shot.title, duration: shot.duration,
    start_frame_prompt: shot.preview_prompt_view?.values.start_frame_prompt ?? shot.prompts?.start_frame_prompt ?? '',
    end_frame_prompt: shot.preview_prompt_view?.values.end_frame_prompt ?? shot.prompts?.end_frame_prompt ?? '',
    video_prompt: shot.preview_prompt_view?.values.video_prompt ?? shot.prompts?.video_prompt ?? '',
  }));
}

export function EpisodePreview({ episode, busy, setBusy, refresh, notify }: {
  episode: Episode; busy: boolean; setBusy: (busy: boolean) => void; refresh: () => void; notify: Notify;
}) {
  const [base, setBase] = useState(episode);
  const [edits, setEdits] = useState(() => editableShots(episode));
  const [dirty, setDirty] = useState(false);
  const [selected, setSelected] = useState(0);
  const [error, setError] = useState<string>();
  const stale = episode.version > base.version;
  const preview = base.preview!;
  const total = edits.reduce((sum, shot) => sum + shot.duration, 0);
  const valid = Number.isFinite(total) && Math.abs(total - base.target_duration) <= 0.005 && edits.every(shot =>
    shot.duration >= preview.min_duration && shot.duration <= preview.max_duration &&
    (preview.fixed_duration == null || Math.abs(shot.duration - preview.fixed_duration) <= 0.011) &&
    shot.title.trim() && shot.start_frame_prompt.trim() && shot.end_frame_prompt.trim() && shot.video_prompt.trim());
  const edit = edits[selected];
  const shot = base.shots[selected];
  const fields: { field: PreviewPromptField; label: string }[] = [
    { field: 'start_frame_prompt', label: '首帧提示词' },
    ...(preview.capability === 'FIRST_LAST_TO_VIDEO' ? [{ field: 'end_frame_prompt' as const, label: '尾帧提示词' }] : []),
    { field: 'video_prompt', label: '视频提示词' },
  ];
  function change(values: Partial<PreviewShotEdit>) {
    setEdits(current => current.map((item, index) => index === selected ? { ...item, ...values } : item));
    setDirty(true); setError(undefined);
  }
  function reload() { setBase(episode); setEdits(editableShots(episode)); setDirty(false); setError(undefined); }
  async function save(start = false) {
    if (!valid || busy || stale) return;
    setBusy(true); setError(undefined);
    try {
      const saved = dirty ? await api<Episode>(`/episodes/${base.id}/preview`, 'PATCH', { expected_version: base.version, shots: edits }) : base;
      setBase(saved); setEdits(editableShots(saved)); setDirty(false);
      if (start) await api(`/episodes/${base.id}/approve`, 'POST', { expected_version: saved.version });
      refresh(); notify(start ? '分镜已确认，开始生成视频' : '分镜修改已保存');
    } catch (problem) { setError((problem as Error).message); refresh(); }
    finally { setBusy(false); }
  }
  function submit(event: FormEvent) { event.preventDefault(); void save(true); }
  return <section className="preview-panel">
    <div className="preview-heading"><div><span className="eyebrow">STORYBOARD REVIEW</span><h2>先看分镜，再开始拍摄。</h2><p>查看镜头节奏和画面提示词。满意后直接开始，也可以先修改并保存。</p></div><span className="preview-step"><Check size={14} />规划完成 · 等待你的确认</span></div>
    <div className="preview-summary"><strong>{edits.length} 个镜头 · 合计 {total.toFixed(2)} / {base.target_duration} 秒</strong><span>单镜 {preview.min_duration}～{preview.max_duration} 秒{preview.fixed_duration != null && ` · 高级设置固定为 ${preview.fixed_duration} 秒`}</span></div>
    <p className="muted preview-note">当前是文字分镜预览，尚未生成图片或视频。提示词会结合视觉设定和实际连续帧使用；修改时长后，请同步检查动作是否适合新的节奏。</p>
    {error && <ErrorNotice>{error}</ErrorNotice>}
    {stale && <div className="notice">分镜已在其他页面更新。当前编辑仍保留，请重新载入后确认。<button disabled={busy} onClick={reload}>放弃本页修改并载入最新分镜</button></div>}
    <form onSubmit={submit}>
      <div className="preview-grid"><aside className="panel preview-shot-list" aria-label="分镜列表">{edits.map((item, index) => <button key={item.id} type="button" className={`preview-shot ${index === selected ? 'selected' : ''}`} aria-pressed={index === selected} onClick={() => setSelected(index)}><span>{String(index + 1).padStart(2, '0')}</span><div><strong>{item.title}</strong><small>{item.duration.toFixed(2)} 秒</small></div></button>)}</aside>
        <div className="panel preview-editor"><div className="panel-title">镜头 {selected + 1} <span>{shot.transition_from_previous}</span></div>
          <div className="fields two"><div className="field"><label htmlFor="preview-title">镜头标题</label><input id="preview-title" value={edit.title} required maxLength={200} disabled={busy} onChange={event => change({ title: event.target.value })} /></div><div className="field"><label htmlFor="preview-duration">镜头时长（秒）</label><input id="preview-duration" type="number" min={preview.min_duration} max={preview.max_duration} step="0.01" required value={Number.isFinite(edit.duration) ? edit.duration : ''} disabled={busy || preview.fixed_duration != null} onChange={event => change({ duration: event.target.value === '' ? NaN : Number(event.target.value) })} /></div></div>
          <div className="preview-direction"><p><strong>画面动作</strong>{shot.action}</p><p><strong>镜头语言</strong>{shot.camera}</p><small>这是导演的原始构思。调整画面或运镜，请修改下方提示词。</small></div>
          {fields.map(({ field, label }) => <div className="field" key={field}><label htmlFor={`preview-${field}`}>{label}</label><textarea id={`preview-${field}`} value={edit[field]} required maxLength={6000} rows={5} disabled={busy || !!shot.preview_prompt_view?.locked[field]} onChange={event => change({ [field]: event.target.value })} />{shot.preview_prompt_view?.locked[field] && <small>{shot.preview_prompt_view.locked[field]}</small>}</div>)}
          <details className="preview-extra"><summary>其他生成约束与 AI 参数（只读）</summary><p>负面提示词：{shot.prompts?.negative_prompt}</p><p>运镜参数：{shot.prompts?.camera_motion} · 运动强度：{shot.prompts?.motion_strength}</p><pre>{JSON.stringify(shot.prompts?.ai_parameters ?? {}, null, 2)}</pre></details>
        </div>
      </div>
      <div className="preview-footer"><div><strong>{dirty ? '有未保存的修改' : '分镜已保存'}</strong><small>{!valid ? `请检查各镜时长与提示词，时长合计须为 ${base.target_duration} 秒。` : '开始生成会自动保存当前修改。'}</small></div><div className="actions"><button type="button" disabled={busy || !dirty || !valid || stale} onClick={() => void save()}><Save size={15} />保存分镜</button><button className="primary" type="submit" disabled={busy || !valid || stale}><Play size={15} />{busy ? '正在处理…' : '开始视频生成'}</button></div></div>
    </form>
  </section>;
}
