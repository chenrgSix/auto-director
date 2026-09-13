import { useRef, useState, type FormEvent } from 'react';
import { Check, Play, Save } from 'lucide-react';
import { api } from './api';
import type { Notify } from './App';
import type { Episode, PreviewPromptField, PreviewShotEdit } from './types';
import { ErrorNotice } from './ui';
import { readShotTiming, retimeShotPrompt, timingIssue } from './shotTiming';
import { ScriptReviewNotice } from './ScriptReviewNotice';

const promptLabels: Record<PreviewPromptField, string> = { start_frame_prompt: '首帧提示词', end_frame_prompt: '尾帧提示词', video_prompt: '视频提示词' };
type PreviewIssue = { message: string; index?: number; field?: keyof PreviewShotEdit };

function previewIssues(episode: Episode, edits: PreviewShotEdit[], total: number): PreviewIssue[] {
  const preview = episode.preview!;
  const issues: PreviewIssue[] = [];
  if (Number.isFinite(total) && Math.abs(total - episode.target_duration) > 0.005) {
    issues.push({ message: `当前合计 ${total.toFixed(2)} 秒，目标 ${episode.target_duration} 秒，${total < episode.target_duration ? '还差' : '超出'} ${Math.abs(total - episode.target_duration).toFixed(2)} 秒。` });
  }
  edits.forEach((shot, index) => {
    const add = (field: keyof PreviewShotEdit, message: string) => issues.push({ index, field, message: `第 ${index + 1} 镜「${shot.title || '未命名'}」：${message}` });
    if (!Number.isFinite(shot.duration)) add('duration', '请填写镜头时长。');
    else if (shot.duration < preview.min_duration || shot.duration > preview.max_duration) add('duration', `时长须为 ${preview.min_duration}～${preview.max_duration} 秒。`);
    else if (preview.fixed_duration != null && Math.abs(shot.duration - preview.fixed_duration) > 0.011) add('duration', `高级设置要求固定 ${preview.fixed_duration} 秒。`);
    if (!shot.title.trim()) add('title', '镜头标题不能为空。');
    for (const field of Object.keys(promptLabels) as PreviewPromptField[]) {
      if (episode.shots[index].preview_prompt_view?.locked[field] || (field === 'end_frame_prompt' && preview.capability === 'IMAGE_TO_VIDEO')) continue;
      if (!shot[field].trim()) add(field, `${promptLabels[field]}不能为空。`);
      else if (shot[field].length > 6000) add(field, `${promptLabels[field]}不能超过 6000 字符。`);
      if (field === 'video_prompt' && Number.isFinite(shot.duration)) {
        const issue = timingIssue(shot[field], shot.duration);
        if (issue) add(field, issue);
      }
    }
  });
  return issues;
}

function editableShots(episode: Episode): PreviewShotEdit[] {
  return episode.shots.map(shot => ({
    id: shot.id, title: shot.title, duration: shot.duration,
    start_frame_prompt: shot.preview_prompt_view?.values.start_frame_prompt ?? shot.prompts?.start_frame_prompt ?? '',
    end_frame_prompt: shot.preview_prompt_view?.values.end_frame_prompt ?? shot.prompts?.end_frame_prompt ?? '',
    video_prompt: shot.preview_prompt_view?.values.video_prompt ?? shot.prompts?.video_prompt ?? '',
    allow_static_end_frame: shot.prompts?.allow_static_end_frame ?? false,
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
  const [reviewAccepted, setReviewAccepted] = useState(false);
  const pendingApproval = useRef<{ request_id: string; expected_version: number; confirm: true } | null>(null);
  const settingsOnly = (episode.quality !== base.quality || episode.qa_policy !== base.qa_policy) && ([
    'id', 'idea', 'title', 'plan', 'bible', 'shots', 'references', 'preview', 'status', 'script_review',
    'preview_approved_at', 'target_duration', 'aspect_ratio', 'style',
    'image_workflow_id', 'video_workflow_id', 'reference_workflow_id',
  ] as const).every(key => JSON.stringify(episode[key]) === JSON.stringify(base[key]));
  if (episode.version > base.version && (!dirty || settingsOnly)) {
    // Saving quality or QA policy must preserve unsaved prompts and advance their version.
    setBase(episode);
    setReviewAccepted(false);
    if (!dirty) setEdits(editableShots(episode));
  }
  const stale = episode.version > base.version;
  const preview = base.preview!;
  const total = edits.reduce((sum, shot) => sum + shot.duration, 0);
  const issues = previewIssues(base, edits, total);
  const valid = issues.length === 0;
  const needsReview = base.script_review?.status === 'needs_attention';
  const canStart = valid && (!needsReview || reviewAccepted);
  const invalidField = (field: keyof PreviewShotEdit) => issues.some(issue => issue.index === selected && issue.field === field);
  const edit = edits[selected];
  const shot = base.shots[selected];
  const timing = readShotTiming(edit.video_prompt);
  const fields: { field: PreviewPromptField; label: string }[] = [
    { field: 'start_frame_prompt', label: '首帧提示词' },
    ...(preview.capability === 'FIRST_LAST_TO_VIDEO' ? [{ field: 'end_frame_prompt' as const, label: '尾帧提示词' }] : []),
    { field: 'video_prompt', label: '视频提示词' },
  ];
  function change(values: Partial<PreviewShotEdit>) {
    setEdits(current => current.map((item, index) => {
      if (index !== selected) return item;
      const adjusted = values.duration !== undefined && !shot.preview_prompt_view?.locked.video_prompt
        ? { video_prompt: retimeShotPrompt(item.video_prompt, values.duration) } : {};
      return { ...item, ...adjusted, ...values };
    }));
    setDirty(true); setError(undefined);
    setReviewAccepted(false);
  }
  function reload() { setBase(episode); setEdits(editableShots(episode)); setDirty(false); setError(undefined); setReviewAccepted(false); }
  async function save(start = false) {
    if (!valid || (start && !canStart) || busy || stale) return;
    setBusy(true); setError(undefined);
    try {
      const saved = dirty ? await api<Episode>(`/episodes/${base.id}/preview`, 'PATCH', { expected_version: base.version, shots: edits }) : base;
      setBase(saved); setEdits(editableShots(saved)); setDirty(false);
      if (start && saved.creation_source) {
        if (pendingApproval.current?.expected_version !== saved.version) {
          pendingApproval.current = { request_id: crypto.randomUUID(), expected_version: saved.version, confirm: true };
        }
        await api(`/creation/projects/${saved.creation_source.project_id}/productions/${saved.id}/confirm`, 'POST', pendingApproval.current);
        pendingApproval.current = null;
      } else if (start) await api(`/episodes/${base.id}/approve`, 'POST', { expected_version: saved.version, accept_script_review: reviewAccepted });
      refresh(); notify(start ? '分镜已确认，开始生成视频' : '分镜修改已保存');
    } catch (problem) { setError((problem as Error).message); refresh(); }
    finally { setBusy(false); }
  }
  function submit(event: FormEvent) { event.preventDefault(); void save(true); }
  return <section className="preview-panel">
    <div className="preview-heading"><div><span className="eyebrow">STORYBOARD REVIEW</span><h2>先看分镜，再开始拍摄。</h2><p>查看镜头节奏和画面提示词。满意后直接开始，也可以先修改并保存。</p></div><span className="preview-step"><Check size={14} />规划完成 · 等待你的确认</span></div>
    <p className="muted">以下时长用于剧本和生成参考；导出完整保留各片段的画面与声音，不按参考时长裁切。</p>
    <div className="preview-summary"><strong>{edits.length} 个镜头 · 参考合计 {Number.isFinite(total) ? total.toFixed(2) : '—'} / {base.target_duration} 秒</strong><span>单镜 {preview.min_duration}～{preview.max_duration} 秒{preview.fixed_duration != null && ` · 高级设置固定为 ${preview.fixed_duration} 秒`}</span></div>
    <p className="muted preview-note">当前是文字分镜预览，尚未生成图片或视频。提示词会结合视觉设定和实际连续帧使用；修改时长后，请同步检查动作是否适合新的节奏。</p>
    <ScriptReviewNotice review={base.script_review} selectShot={setSelected} busy={busy} />
    {error && <ErrorNotice>{error}</ErrorNotice>}
    {!!issues.length && <div className="notice preview-validation" role="alert"><strong>开始前请处理以下问题</strong><ul>{issues.map((issue, index) => <li key={index}>{issue.index == null ? issue.message : <button className="text-button" type="button" onClick={() => setSelected(issue.index!)}>{issue.message} 查看此镜</button>}</li>)}</ul></div>}
    {stale && <div className="notice">分镜已在其他页面更新。当前编辑仍保留，请重新载入后确认。<button disabled={busy} onClick={reload}>放弃本页修改并载入最新分镜</button></div>}
    <form onSubmit={submit}>
      <div className="preview-grid"><aside className="panel preview-shot-list" aria-label="分镜列表">{edits.map((item, index) => <button key={item.id} type="button" className={`preview-shot ${index === selected ? 'selected' : ''}`} aria-pressed={index === selected} onClick={() => setSelected(index)}><span>{String(index + 1).padStart(2, '0')}</span><div><strong>{item.title}</strong><small>{item.duration.toFixed(2)} 秒</small></div></button>)}</aside>
        <div className="panel preview-editor"><div className="panel-title">镜头 {selected + 1} <span>{shot.transition_from_previous}</span></div>
          <div className="fields two"><div className="field"><label htmlFor="preview-title">镜头标题</label><input id="preview-title" value={edit.title} required maxLength={200} aria-invalid={invalidField('title')} disabled={busy} onChange={event => change({ title: event.target.value })} /></div><div className="field"><label htmlFor="preview-duration">镜头时长（秒）</label><input id="preview-duration" type="number" min={preview.min_duration} max={preview.max_duration} step="0.01" required value={Number.isFinite(edit.duration) ? edit.duration : ''} aria-invalid={invalidField('duration')} disabled={busy || preview.fixed_duration != null} onChange={event => change({ duration: event.target.value === '' ? NaN : Number(event.target.value) })} /></div></div>
          <div className="preview-direction"><p><strong>画面动作</strong>{shot.action}</p><p><strong>镜头语言</strong>{shot.camera}</p><small>这是当前剧本的镜头设计。调整画面或运镜，请修改下方提示词。</small></div>
          <div className="preview-direction"><strong>镜内动作节奏</strong>{timing ? <ol aria-label="镜内动作时间线">{timing.beats.map((beat, index) => <li key={index}><strong>{beat.start}–{beat.end} 秒</strong> {beat.action}</li>)}</ol> : <p>当前未单独分段。短镜头可只写一个清晰动作；已有提示词保持原样。</p>}<small>4 秒以上的新分镜按动作需要分段。可在下方视频提示词末尾修改时间和动作；调整时长会同步缩放已有时间段。时间是生成指引，实际动作节奏取决于视频模型。</small></div>
          {fields.map(({ field, label }) => <div className="field" key={field}><label htmlFor={`preview-${field}`}>{label}</label><textarea id={`preview-${field}`} value={edit[field]} required maxLength={6000} rows={5} aria-invalid={invalidField(field)} disabled={busy || !!shot.preview_prompt_view?.locked[field]} onChange={event => change({ [field]: event.target.value })} />{shot.preview_prompt_view?.locked[field] && <small>{shot.preview_prompt_view.locked[field]}</small>}{shot.preview_prompt_view?.hints?.[field] && <small>{shot.preview_prompt_view.hints[field]}</small>}</div>)}
          {preview.capability === 'FIRST_LAST_TO_VIDEO' && <label className="check"><input type="checkbox" checked={edit.allow_static_end_frame} disabled={busy} onChange={event => change({ allow_static_end_frame: event.target.checked })} />允许静止首尾帧（仅用于有意定格的镜头）</label>}
          {shot.prompts?.narration_text && <div className="preview-extra"><strong>旁白脚本参考</strong><p>{shot.prompts.narration_text}</p><small>支持原生声音的工作流根据视频提示词生成台词；实际使用的内容以可编辑视频提示词为准。旧脚本不会自动补声。</small></div>}
          <details className="preview-extra"><summary>其他生成约束与 AI 参数（只读）</summary><p>负面提示词：{shot.prompts?.negative_prompt}</p><p>运镜参数：{shot.prompts?.camera_motion} · 运动强度：{shot.prompts?.motion_strength}</p><pre>{JSON.stringify(shot.prompts?.ai_parameters ?? {}, null, 2)}</pre></details>
        </div>
      </div>
      {needsReview && <label className="check script-review-ack"><input type="checkbox" checked={reviewAccepted} disabled={busy || stale} onChange={event => setReviewAccepted(event.target.checked)} />我已检查 AI 提出的剧本问题，确认按当前分镜生成</label>}
      <div className="preview-footer"><div><strong>{dirty ? '有未保存的修改' : '分镜已保存'}</strong><small>{issues[0]?.message ?? (needsReview && !reviewAccepted ? '请先检查并确认 AI 提出的剧本问题。' : '开始生成会自动保存当前修改。')}</small></div><div className="actions"><button type="button" disabled={busy || !dirty || !valid || stale} onClick={() => void save()}><Save size={15} />保存分镜</button><button className="primary" type="submit" disabled={busy || !canStart || stale}><Play size={15} />{busy ? '正在处理…' : '开始视频生成'}</button></div></div>
    </form>
  </section>;
}
