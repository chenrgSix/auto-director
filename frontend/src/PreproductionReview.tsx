import { useRef, useState } from 'react';
import { api, useResource } from './api';
import type { Episode } from './types';
import { ErrorNotice } from './ui';

type Context = {
  version: number; review_key: string; stage: 'references' | 'keyframes'; shot_id: string | null; limitations: string;
  frames: { target: string; asset_id: string; url: string; editable: boolean; prompt?: string }[];
};
type Decision = { request_id: string; expected_version: number; review_key: string; decision: 'approve' | 'revise'; target: string | null; prompt: string; notes: string };

function label(target: string) {
  return ({ start_frame: '本镜首帧', end_frame: '本镜尾帧', previous_actual_end: '前镜真实尾帧', environment: '场景参考', style: '风格参考' } as Record<string, string>)[target] ?? target.replace('character:', '人物 · ').replace('prop:', '道具 · ');
}

export function PreproductionReview({ episode, refresh }: { episode: Episode; refresh: () => void }) {
  const sequence = episode.production_mode === 'reference_sequence';
  const path = `/episodes/${episode.id}/image-review`;
  const resource = useResource<Context>(path);
  const context = resource.data;
  const [notes, setNotes] = useState('');
  const [target, setTarget] = useState('');
  const [prompt, setPrompt] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [loaded, setLoaded] = useState<string[]>([]);
  const receipt = useRef<Decision | null>(null);
  async function save(decision: 'approve' | 'revise') {
    if (!context) return;
    const values = { expected_version: context.version, review_key: context.review_key, decision, target: decision === 'revise' ? target : null, prompt: decision === 'revise' ? prompt.trim() : '', notes: notes.trim() };
    if (!receipt.current || JSON.stringify({ ...receipt.current, request_id: '' }) !== JSON.stringify({ request_id: '', ...values })) receipt.current = { request_id: crypto.randomUUID(), ...values };
    setBusy(true); setError(undefined);
    try { await api(path, 'POST', receipt.current); receipt.current = null; refresh(); }
    catch (cause) { setError((cause as Error).message); resource.refresh(); refresh(); }
    finally { setBusy(false); }
  }
  const shot = episode.shots.find(s => s.id === context?.shot_id);
  return <section className="panel details-panel preproduction-review">
    <h2>{context?.stage === 'keyframes' ? `第 ${(shot?.index ?? 0) + 1} 镜：确认首尾帧` : '确认人物、场景与道具参考'}</h2>
    <p>制作已暂停。检查人物身份、空间布局、道具形状和动作起止位置；画面不符合预期时，可修改后只重新生成目标图片。</p>
    {(resource.error || error) && <ErrorNotice>{error ?? resource.error}</ErrorNotice>}
    {context && <><div className="continuity-frames">{context.frames.map(frame => <figure key={frame.asset_id + frame.target}>
      <img src={frame.url} alt={label(frame.target)} onLoad={() => setLoaded(current => current.includes(frame.asset_id) ? current : [...current, frame.asset_id])} />
      <figcaption>{label(frame.target)}</figcaption>
      {frame.prompt && <details><summary>目标画面描述</summary><p>{frame.prompt}</p></details>}
    </figure>)}</div>
      <label className="field">画面观察<textarea value={notes} maxLength={3000} onChange={event => setNotes(event.target.value)} placeholder="说明已经核对的特征，或当前画面哪里需要修改。" /></label>
      <details><summary>修改目标图片</summary><label className="field">选择图片<select value={target} onChange={event => { setTarget(event.target.value); setPrompt(context.frames.find(f => f.target === event.target.value)?.prompt ?? ''); }}><option value="">请选择</option>{context.frames.filter(frame => frame.editable).map(frame => <option key={frame.target} value={frame.target}>{label(frame.target)}</option>)}</select></label>
        <label className="field">完整视觉提示词<textarea rows={4} maxLength={6000} value={prompt} onChange={event => setPrompt(event.target.value)} placeholder="完整描述希望得到的画面，只写可见内容。" /></label>
        <p className="muted">{sequence ? '修改只会重新生成选中的参考图，旧图保留。视频使用确认后的参考图，不生成逐镜首尾帧。' : '修改首帧会同时重做尾帧。连续镜头的首帧来自前镜真实结尾，不能在这里单独重画。旧图保存在素材记录中。'}</p>
        <button disabled={busy || !target || !prompt.trim() || !notes.trim()} onClick={() => void save('revise')}>修改并重新生成图片</button>
      </details>
      <div className="actions"><button className="primary" disabled={busy || !notes.trim() || !context.frames.every(frame => loaded.includes(frame.asset_id))} onClick={() => void save('approve')}>{context.stage === 'references' ? sequence ? '确认参考图，生成连续视频' : '确认参考图，生成关键帧' : '确认关键帧，生成本镜视频'}</button><button disabled={busy} onClick={() => resource.refresh()}>刷新画面</button></div><small className="muted">{context.limitations}</small>
    </>}
  </section>;
}
